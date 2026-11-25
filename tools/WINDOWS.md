# Локальный запуск на MSI Katana 17 HX B14WFK

Проверка 25 ноября 2026 года. Intel Core i5-14450HX, 16 логических процессоров. RTX 5060 Laptop GPU, 8151 МиБ VRAM, драйвер 621.24. Windows видела 15,71 ГиБ RAM, из них при проверке свободно 6,94 ГиБ. На C: доступно 692,72 ГиБ из 942,88 ГиБ. На D: доступно 3,31 ГиБ из 10 ГиБ. Проверенная вычислительная архитектура GPU — CUDA capability 12.0. Используется проект на C:.

| Этап | Выбранный локальный вариант |
| --- | --- |
| Сценарии | Ollama `qwen3:4b`, 2,5 ГБ, контекст студии 8192, без thinking для JSON |
| Иллюстрации | SDXL Base 1.0 + IP-Adapter Plus SDXL ViT-H + CLIP ViT-H, batch 1, 1024×576, режим `--lowvram` и 20 шагов |
| Озвучка | Piper 1.8.0, `ru_RU-dmitri-medium`, CPU, один русский голос |
| Монтаж | Установленные FFmpeg и ffprobe, MP4 1920×1080 |

Ollama освобождает модель по завершении запроса. Run-проверка запускает модели по очереди. При нехватке памяти закройте другие GPU-задачи и для следующей попытки уменьшите размер латента в копии workflow до 768×432. Ожидаемый суммарный трафик до 20 ГБ, резерв свободного места 35 ГБ. Бюджет $0. Скрипт не меняет PATH, драйверы, автозапуск и системные службы.

Из корня проекта на Windows:

```powershell
python tools/setup_windows.py check
python tools/setup_windows.py configure
python tools/setup_windows.py piper
python tools/setup_windows.py full
python tools/setup_windows.py voice-test
python tools/setup_windows.py probe
python tools/setup_windows.py pilot
```

`full` скачивает согласованные крупные файлы. Скачанное и среды остаются в `.studio-local/`, защищённом .gitignore. На этом ноутбуке уже есть проверенный CUDA PyTorch 2.11.0+cu128 в изолированном окружении другого проекта. Опциональный `--reuse-torch-python` копирует CUDA-пакеты из указанного локального окружения в окружение студии. Исходное окружение остаётся доступным в прежнем виде. Повтор команды использует уже скачанные файлы, недокачанные архивы и веса имеют суффикс `.part`. У весов, голоса и пакета Ollama проверяется SHA-256. Команды `ollama` и `comfy` устанавливают части по отдельности. `probe` отдельно проверяет русскоязычный JSON Ollama, граф ComfyUI и дорожки Piper. Журналы локальных серверов находятся в `.studio-local/`. Серверы слушают только 127.0.0.1 на портах 11435 и 8189, останавливаются после использования.

Создание пилота использует семь заранее подготовленных планов из начала главы 001, проверенные дословные опорные цитаты и семь новых кадров, обусловленных утверждённым PNG. Принят русский язык для диктора, субтитров и метаданных. Текст запросов к модели изображений оставлен на английском. Перед монтажом проверяется длительность 30–60 секунд. Исходный эталон находится в `assets/characters/jinhyeok-concept.jpg`. Команды `configure` и `pilot` создают из его фронтального вида локальный `jinhyeok-approved.png`, если файл ещё отсутствует. Референс можно указать в локальном `.env`. [Раскадровка](../youtube/storyboards/001-pilot.plan.json)

Источники и лицензии: [Qwen3 в Ollama](https://ollama.com/library/qwen3), [Ollama для Windows](https://ollama.com/download/windows), [ComfyUI](https://docs.comfy.org/installation/manual_install), [PyTorch CUDA 12.8](https://pytorch.org/get-started/previous-versions/), [SDXL Base](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0), [IP-Adapter Plus](https://github.com/cubiq/ComfyUI_IPAdapter_plus), [карточка голоса Дмитрия](https://huggingface.co/rhasspy/piper-voices/blob/main/ru/ru_RU/dmitri/medium/MODEL_CARD).
