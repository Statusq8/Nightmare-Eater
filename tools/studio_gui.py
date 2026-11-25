"""Optional desktop launcher for the same studio CLI."""
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk


def main():
    root = tk.Tk()
    root.title("Nightmare Eater — локальная студия")
    root.geometry("790x480")
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    chapter = tk.StringVar(value="001")
    shots = tk.StringVar(value="24")
    ttk.Label(frame, text="Глава").grid(row=0, column=0, sticky="w")
    ttk.Entry(frame, textvariable=chapter, width=6).grid(row=0, column=1, sticky="w")
    ttk.Label(frame, text="Макс. кадров").grid(row=0, column=2, sticky="w")
    ttk.Entry(frame, textvariable=shots, width=6).grid(row=0, column=3, sticky="w")
    output = tk.Text(frame, wrap="word", state="disabled", height=18)
    scroll = ttk.Scrollbar(frame, command=output.yview)
    output.configure(yscrollcommand=scroll.set)
    output.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=8)
    scroll.grid(row=2, column=4, sticky="ns", pady=8)
    frame.rowconfigure(2, weight=1)
    frame.columnconfigure(3, weight=1)
    events = queue.Queue()
    busy = False

    def worker(command):
        try:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as proc:
                for line in proc.stdout:
                    events.put(line)
                events.put(f"\nКод завершения: {proc.wait()}\n")
        except OSError as error:
            events.put(str(error) + "\n")
        finally:
            events.put(None)

    def launch(flags):
        nonlocal busy
        if busy:
            return
        busy = True
        output.configure(state="normal")
        output.delete("1.0", tk.END)
        output.configure(state="disabled")
        command = [sys.executable, "-X", "utf8", "-u", str(Path(__file__).with_name("studio.py")),
                   chapter.get(), "--max-shots", shots.get(), *flags]
        threading.Thread(target=worker, args=(command,), daemon=True).start()

    def drain():
        nonlocal busy
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event is None:
                busy = False
            else:
                output.configure(state="normal")
                output.insert(tk.END, event)
                output.see(tk.END)
                output.configure(state="disabled")
        root.after(100, drain)

    def close():
        if busy:
            messagebox.showinfo("Студия", "Дождитесь завершения текущей команды.")
        else:
            root.destroy()

    for col, (label, args) in enumerate((
        ("Проверка настроек", []), ("Синтетический тест", ["--synthetic", "--run"]),
        ("Собрать / продолжить", ["--run"]),
    )):
        ttk.Button(frame, text=label, command=lambda flags=args: launch(flags)).grid(
            row=1, column=col, columnspan=1, sticky="w", padx=(0, 8), pady=(8, 0))
    root.protocol("WM_DELETE_WINDOW", close)
    drain()
    root.mainloop()


if __name__ == "__main__":
    main()
