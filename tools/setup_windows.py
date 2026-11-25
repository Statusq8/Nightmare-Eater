"""Install and run the Windows studio in a project-local directory."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from studio import ROOT, blockers, probe, read, settings, validate_plan
from studio_providers import Piper, local_call

LOCAL = ROOT / ".studio-local"
CACHE = LOCAL / "downloads"
OLLAMA = LOCAL / "ollama" / "ollama.exe"
COMFY = LOCAL / "ComfyUI"
COMFY_PY = LOCAL / "comfy-venv" / "Scripts" / "python.exe"
PIPER_PY = LOCAL / "piper" / "Scripts" / "python.exe"
VOICE = LOCAL / "piper-models" / "ru_RU-dmitri-medium.onnx"
OLLAMA_URL, COMFY_URL = "http://127.0.0.1:11435", "http://127.0.0.1:8189"
COMFY_SHA = "a277b5bf213e8a44fa34817b7770b54f86ef4cea"
ADAPTER_SHA = "a0f451a5113cf9becb0847b92884cb10cbdec0ef"
OLLAMA_ZIP = "https://github.com/ollama/ollama/releases/download/v0.40.18/ollama-windows-amd64.zip"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/5ec43b186ef915a195d3ddbcfd9916e76c503c3f/ru/ru_RU/dmitri/medium/"
VOICE_HASHES = {
    VOICE.name: "9ed9941bac7373f2ec1be36627e0e71a349a59ca5522d4363fd50701db05193a",
    VOICE.name + ".json": "c3741c9c1558fc38e902fd80d39155c32a7d019d7d17e3a43eac66b7bd3fbe09",
}
PIPER_WHEEL = (
    "https://files.pythonhosted.org/packages/12/9c/c736d1961cf9ce0655278731b9430df41892ca2ded7b09dabcb340612158/piper_tts-1.8.0-cp39-abi3-win_amd64.whl",
    "piper_tts-1.8.0-cp39-abi3-win_amd64.whl",
    "5da9bfdb05dfe15da3536859d422e605483ffa6d2b3ec2c5b9593bae6b5aa6a4", 34119688,
)
TORCH_WHEEL = (
    "https://download-r2.pytorch.org/whl/cu128/torch-2.10.0%2Bcu128-cp312-cp312-win_amd64.whl",
    "torch-2.10.0+cu128-cp312-cp312-win_amd64.whl",
    "fbde8f6a9ec8c76979a0d14df21c10b9e5cab6f0d106a73ca73e2179bc597cae", 2867409626,
)
WEIGHTS = [
    ("checkpoints", "sd_xl_base_1.0.safetensors",
     "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/462165984030d82259a11f4367a4eed129e94a7b/sd_xl_base_1.0.safetensors",
     "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b", 6938078334),
    ("clip_vision", "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
     "https://huggingface.co/h94/IP-Adapter/resolve/018e402774aeeddd60609b4ecdb7e298259dc729/models/image_encoder/model.safetensors",
     "6ca9667da1ca9e0b0f75e46bb030f7e011f44f86cbfb8d5a36590fcd7507b030", 2528373448),
    ("ipadapter", "ip-adapter-plus_sdxl_vit-h.safetensors",
     "https://huggingface.co/h94/IP-Adapter/resolve/018e402774aeeddd60609b4ecdb7e298259dc729/sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors",
     "3f5062b8400c94b7159665b21ba5c62acdcd7682262743d7f2aefedef00e6581", 847517512),
]


def call(command, **kwargs):
    subprocess.run([str(item) for item in command], check=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def range_download(url, part, total):
    chunk_size = 16 * 1024 * 1024
    initial = part.stat().st_size if part.exists() else 0
    if initial > total:
        raise ValueError(f"Partial file is larger than expected: {part}")

    def fetch(bounds):
        start, end = bounds
        target = part.with_name(part.name + f".{start}.chunk")
        size = end - start + 1
        if not target.is_file() or target.stat().st_size != size:
            status = subprocess.check_output([
                "curl.exe", "-fsSL", "--retry", "2", "--connect-timeout", "20", "--max-time", "180",
                "--range", f"{start}-{end}", "--max-filesize", str(size), "--output", str(target),
                "--write-out", "%{http_code}", url,
            ], text=True)
            if status.strip() != "206" or target.stat().st_size != size:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"Server did not return the requested byte range for {part.name}.")
        return target

    entries = [(start, min(start + chunk_size, total) - 1) for start in range(initial, total, chunk_size)]
    with ThreadPoolExecutor(max_workers=4) as pool, part.open("ab") as output:
        for offset in range(0, len(entries), 8):
            for target in pool.map(fetch, entries[offset:offset + 8]):
                with target.open("rb") as source:
                    shutil.copyfileobj(source, output)
                output.flush()
                target.unlink()
            print(f"{part.name}: {output.tell()/1e6:.0f}/{total/1e6:.0f} MB", flush=True)


def download(url, name, digest=None, size=None):
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / name
    if target.is_file():
        if digest and sha256(target) != digest:
            raise ValueError(f"Wrong SHA256: {target}")
        return target
    part = target.with_suffix(target.suffix + ".part")
    if size:
        range_download(url, part, size)
    else:
        call(["curl.exe", "--fail", "--location", "--retry", "3", "--retry-all-errors",
              "--connect-timeout", "20", "--speed-limit", "32768", "--speed-time", "45",
              "--max-time", "5400", "--continue-at", "-", "--output", part, url])
    if digest and sha256(part) != digest:
        raise ValueError(f"Wrong SHA256: {part}")
    part.replace(target)
    return target


def extract(archive, target, inner=None):
    if target.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        scratch = Path(temp)
        content = scratch / "content"
        with zipfile.ZipFile(archive) as source:
            source.extractall(content)
        (content / inner if inner else content).replace(target)


def init_env():
    target = ROOT / ".env"
    if not target.exists():
        shutil.copyfile(ROOT / ".env.example", target)
        print(f"Configured {target}")
    else:
        print(f"Using existing {target}")


def hardware():
    import ctypes
    import winreg

    def registry(subkey, key):
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "HARDWARE\\DESCRIPTION\\System\\" + subkey) as reg:
            return winreg.QueryValueEx(reg, key)[0]

    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (name, ctypes.c_uint64) for name in ("total", "free", "page", "page_free", "virtual",
                                                "virtual_free", "extended")
        ]

    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        print(f"RAM visible/free: {memory.total / 2**30:.2f}/{memory.free / 2**30:.2f} GiB")
    print(registry("BIOS", "SystemProductName"))
    print(registry("CentralProcessor\\0", "ProcessorNameString"), f"({os.cpu_count()} logical CPUs)")
    if shutil.which("nvidia-smi"):
        call(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv"])

def create_environment(executable):
    if not executable.is_file():
        call([sys.executable, "-m", "venv", executable.parent.parent])


def pip_install(python, *args):
    env = {**os.environ, "PIP_CACHE_DIR": str(LOCAL / "pip-cache")}
    call([python, "-m", "pip", "install", "--disable-pip-version-check",
          "--timeout", "60", "--retries", "3", *args], env=env)


def install_piper():
    create_environment(PIPER_PY)
    pip_install(PIPER_PY, download(*PIPER_WHEEL))
    VOICE.parent.mkdir(parents=True, exist_ok=True)
    for name, digest in VOICE_HASHES.items():
        target = VOICE.parent / name
        if not target.is_file():
            shutil.copyfile(download(VOICE_URL + name, name, digest), target)
        if sha256(target) != digest:
            raise ValueError(f"Wrong SHA256: {target}")
    init_env()


def ollama_env():
    return {**os.environ, "OLLAMA_NO_CLOUD": "1", "OLLAMA_HOST": "127.0.0.1:11435",
            "OLLAMA_MODELS": str(LOCAL / "ollama-models"), "OLLAMA_NUM_PARALLEL": "1"}


@contextmanager
def server(name, url, endpoint, command, cwd, env=None):
    try:
        local_call(url, endpoint)
    except (OSError, RuntimeError):
        pass
    else:
        raise RuntimeError(f"Port already has a server for {name}: {url}")
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / (name + ".log")).open("ab") as log:
        process = subprocess.Popen([str(item) for item in command], cwd=cwd, env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError(f"{name} stopped. See {log.name}.")
                try:
                    local_call(url, endpoint)
                    break
                except (OSError, RuntimeError):
                    time.sleep(1)
            else:
                raise TimeoutError(f"{name} did not start. See {log.name}.")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def run_ollama():
    return server("ollama", OLLAMA_URL, "/api/version", [OLLAMA, "serve"], OLLAMA.parent, ollama_env())


def run_comfy():
    return server("comfy", COMFY_URL, "/system_stats",
                  [COMFY_PY, COMFY / "main.py", "--listen", "127.0.0.1", "--port", "8189",
                   "--lowvram", "--preview-method", "none"], COMFY)


def install_ollama():
    extract(download(OLLAMA_ZIP, "ollama-windows-amd64.zip",
                     "ca97292ac1e9acc77fcf473bb2741fa6acd12e3798f261b419680068b7cc4c16", 1717307988), OLLAMA.parent)
    with run_ollama():
        call([OLLAMA, "pull", "qwen3:4b"], env=ollama_env())


def copy_cuda_runtime(source_python):
    import json
    import re
    info = json.loads(subprocess.check_output([str(source_python), "-c",
        "import json,sys,site,torch; assert torch.cuda.is_available(); "
        "print(json.dumps([list(sys.version_info[:2]), site.getsitepackages()[-1], torch.__version__]))"],
        text=True))
    version, source, torch_version = info
    if version != list(sys.version_info[:2]) or "+cu" not in torch_version:
        raise ValueError("Reuse requires a CUDA PyTorch runtime with the same Python ABI.")
    source = Path(source)
    destination = COMFY_PY.parent.parent / "Lib" / "site-packages"
    for path in sorted(source.iterdir()):
        if path.name in {"torch", "torchgen", "functorch", "torchvision", "torchaudio", "torchvision.libs",
                          "torchaudio.libs"} or re.fullmatch(r"(torch|torchvision|torchaudio)-.*\.dist-info", path.name):
            target = destination / path.name
            if not target.exists():
                with tempfile.TemporaryDirectory(dir=destination) as temp:
                    staging = Path(temp) / path.name
                    shutil.copytree(path, staging)
                    staging.replace(target)
    print(f"Reused {torch_version} in the isolated ComfyUI environment.")


def install_comfy(reuse_torch=None):
    extract(download(f"https://codeload.github.com/Comfy-Org/ComfyUI/zip/{COMFY_SHA}", "ComfyUI.zip"),
            COMFY, f"ComfyUI-{COMFY_SHA}")
    adapter = COMFY / "custom_nodes" / "ComfyUI_IPAdapter_plus"
    extract(download(f"https://codeload.github.com/cubiq/ComfyUI_IPAdapter_plus/zip/{ADAPTER_SHA}",
                     "ComfyUI_IPAdapter_plus.zip"), adapter, f"ComfyUI_IPAdapter_plus-{ADAPTER_SHA}")
    create_environment(COMFY_PY)
    with ThreadPoolExecutor(max_workers=1) as pool:
        model_install = pool.submit(install_weights)
        if reuse_torch:
            copy_cuda_runtime(reuse_torch)
        elif not any((COMFY_PY.parent.parent / "Lib/site-packages").glob("torch-*+cu128.dist-info")):
            pip_install(COMFY_PY, download(*TORCH_WHEEL), "torchvision==0.25.0", "torchaudio==2.10.0",
                        "--index-url", "https://download.pytorch.org/whl/cu128")
        pip_install(COMFY_PY, "-r", COMFY / "requirements.txt")
        if (adapter / "requirements.txt").is_file():
            pip_install(COMFY_PY, "-r", adapter / "requirements.txt")
        model_install.result()
    call([COMFY_PY, "-c", "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name())"])


def install_weights():
    for group, name, url, digest, size in WEIGHTS:
        target = COMFY / "models" / group / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            source = download(url, name, digest, size)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
        if sha256(target) != digest:
            raise ValueError(f"Wrong SHA256: {target}")


def install_all(reuse_torch=None):
    if shutil.disk_usage(ROOT).free < 35_000_000_000:
        raise RuntimeError("At least 35 GB free is required for downloads and unpacking.")
    install_piper()
    install_ollama()
    install_comfy(reuse_torch)


def check_pilot_voice():
    import json

    plan = json.loads(read(ROOT / "youtube/storyboards/001-pilot.plan.json"))
    validate_plan(plan, read(ROOT / "chapters/001.md"), 7)
    piper = Piper(settings())
    with tempfile.TemporaryDirectory(dir=LOCAL) as scratch:
        root = Path(scratch)
        duration = 0
        for index, shot in enumerate(plan["shots"], 1):
            wav = root / f"p{index}.wav"
            piper.speech(shot["narration"], wav)
            duration += float(probe(wav)["format"]["duration"]) + 0.2
    if not 30 <= duration <= 60:
        raise RuntimeError(f"Unexpected pilot duration: {duration:.2f}s")
    print(f"Seven recorded beats, approx. {duration:.2f}s with pauses")


def missing_runtime():
    errors = []
    comfy_site = COMFY_PY.parent.parent / "Lib/site-packages"
    piper_site = PIPER_PY.parent.parent / "Lib/site-packages"
    if not (comfy_site / "torch").is_dir() or not (comfy_site / "aiohttp").is_dir():
        errors.append("ComfyUI Python dependencies are not installed.")
    if not (COMFY / "main.py").is_file() or not (
        COMFY / "custom_nodes/ComfyUI_IPAdapter_plus/IPAdapterPlus.py"
    ).is_file():
        errors.append("ComfyUI or IP-Adapter source is missing.")
    errors.extend(f"Missing ComfyUI weight: {name}" for group, name, _, _, _ in WEIGHTS
                  if not (COMFY / "models" / group / name).is_file())
    if not (piper_site / "piper").is_dir():
        errors.append("Piper runtime is not installed.")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "configure", "piper", "ollama", "comfy", "full", "voice-test", "probe", "pilot"),
                        nargs="?", default="check")
    parser.add_argument("--reuse-torch-python", type=Path, help="Copy CUDA wheels from another local venv")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This installer targets Windows.")
    if args.action == "check":
        hardware()
        for name, path in (("Ollama", OLLAMA), ("ComfyUI", COMFY / "main.py"),
                           ("Comfy Python", COMFY_PY), ("Piper", PIPER_PY), ("Russian voice", VOICE)):
            print(f"{name}: {'found' if path.is_file() else 'missing'}")
        for error in missing_runtime():
            print(error)
        print(f"Disk free: {shutil.disk_usage(ROOT).free / 2**30:.1f} GiB")
        print("Full install estimate: up to 20 GB downloads and 35 GB temporary free space, USD 0.")
    elif args.action == "configure":
        init_env()
    elif args.action == "piper":
        install_piper()
    elif args.action == "ollama":
        init_env()
        install_ollama()
    elif args.action == "comfy":
        init_env()
        install_comfy(args.reuse_torch_python)
    elif args.action == "full":
        install_all(args.reuse_torch_python)
    elif args.action == "voice-test":
        check_pilot_voice()
    elif args.action == "probe":
        config = settings()
        from studio_providers import ComfyUI, Ollama
        with run_ollama():
            result = Ollama(config).generate(config["ollama_model"], "Return JSON only.",
                                             "Ответь объектом JSON с полем ok и значением true.")
            if result.get("ok") is not True:
                raise RuntimeError("Ollama returned unexpected JSON.")
            print("Ollama: Russian JSON test passed.")
        with run_comfy():
            ComfyUI(config).validate()
            print("ComfyUI: workflow and local nodes validated.")
        check_pilot_voice()
    else:
        config = settings()
        config["text_provider"] = "prepared"
        config["prepared_dir"] = str(ROOT / "youtube" / "storyboards")
        errors = blockers(config) + missing_runtime()
        if errors:
            parser.exit(2, "\n".join(errors) + "\n")
        with run_comfy():
            call([sys.executable, ROOT / "tools" / "studio.py", "001", "--pilot", "--run"], cwd=ROOT)


if __name__ == "__main__":
    main()
