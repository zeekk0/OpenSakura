# Troubleshooting

Start with the health report. It lists what is wrong and often the exact command that fixes it:

- Windows: `.venv\Scripts\python launch.py --check`
- macOS / Linux: `.venv/bin/python launch.py --check`

`OK` = fine, `!!` = fix this, `--` = information only. The page's **Setup -> Health** shows the same but with coloured dots instead.
Logs of every service are in the `logs/` folder (`stt.log`, `tts.log`); the newest lines are usually the answer.

## Installing

| symptom | what to do |
| --- | --- |
| `OpenSakura needs Python 3.11 (recommended) or 3.12` | Install Python 3.11 *beside* the one you have (nothing is replaced): python.org (Windows: tick *Add python.exe to PATH*), `winget install Python.Python.3.11`, `brew install python@3.11`, `sudo apt install python3.11 python3.11-venv`, or on any system `uv python install 3.11`. Close and reopen the terminal, run the setup again. Or pass `--python <path>`. |
| downloads from Hugging Face are slow or blocked | Set `HF_ENDPOINT=https://hf-mirror.com` (Windows PowerShell: `$env:HF_ENDPOINT='https://hf-mirror.com'`) and run the setup again. |
| `ensurepip is not available` / `No module named venv` (Linux) | `sudo apt install python3.11-venv` (or `python3.12-venv` if you use 3.12). |
| setup stopped part-way (network error, closed window) | Run the same command again. Finished steps are skipped. |
| the PyTorch download is huge | y e s, ofc it's huge (~3GB) cuz it's CUDA. No NVIDIA GPU? Use `python scripts/setup.py --device cpu` for small CPU-only wheels. |
| AMD GPU: `No matching distribution found for torch` with ROCm | The ROCm wheel tag changes with PyTorch releases. Look up the current one on pytorch.org and pass `--torch-index https://download.pytorch.org/whl/rocmX.Y`. |
| macOS: voice setup says macOS too old | The voice engine needs macOS 14+ on Apple Silicon. Text-only mode works anywhere. |
| model download fails | Hugging Face must be reachable. Re-run; or `python scripts/download_sensevoice.py`. |
| `Permission denied` on `.bat` files | `chmod +x Start_OpenSakura.bat scripts/setup.bat` or run them with `bash`. |

## Starting

| symptom | what to do |
| --- | --- |
| browser does not open | Open http://localhost:8010 yourself. |
| `port 8010 is busy` | Another copy is running (close it), or change `server.port` in `config/settings.json`. |
| services keep running after closing the window (Linux/macOS) | Stop them with (spamming) Ctrl+C in the launcher window. If it was killed hard: `pkill -f "services/stt/api.py"; pkill -f "tts-engine"`. |

## She does not answer / the brain

| symptom | what to do |
| --- | --- |
| "no API key" | Put `OPENROUTER_API_KEY=...` in `.env` and restart, or choose another brain in **Setup -> LLM**. |
| Ollama row is `!!` | Start Ollama; `ollama pull <model>` for any missing model. |
| OpenAI-compatible row is `!!` | The server (llama-server, LM Studio, ...) is not running, or the URL is wrong. It must end in `/v1`. See [OPENAI_COMPATIBLE.md](OPENAI_COMPATIBLE.md). |
| answers are slow | Use a smaller or non-"thinking" model; on CPU prefer 4B models or OpenRouter. With llama.cpp, add `"extra_body": {"chat_template_kwargs": {"enable_thinking": false}}` under `llm.openai`. |

## Voice (ears and mouth)

| symptom | what to do |
| --- | --- |
| no sound | Click the page once (browsers block autoplay); tick **play voice**; **TTS** must be green in Health; wait ~30 s after start. On Safari keep the WAV format. |
| microphone does nothing | Allow the microphone for the page (browser address bar icon). Needs `http://localhost` or HTTPS, not a plain `http://<LAN-IP>`. |
| `stt` log: `requested device 'cuda:0' is not available ... using 'cpu'` | Fine. No CUDA GPU here, so it runs on the CPU. Set `stt.device` to `auto` to silence it. |
| speech recognition is slow | On a CPU-only machine set `stt.threads` to your number of physical cores; close heavy apps. It is a small model and usually still fast enough for chat-length sentences. |
| her voice takes many seconds per sentence | You are probably on CPU. Check `logs/tts.log` for `device cpu`. On CPU set `tts.extra_args` to `["--batch-size", "1"]`; shorter replies help most. A GPU makes this much faster. |
| TTS on AMD / Intel GPU fails | Support is experimental. Set `tts.device` to `cpu` and re-run `setup_tts.py --device cpu`. |
| the voice licence prompt never appears | Run `python scripts/setup_tts.py` from a real terminal (it must be interactive to ask). |

## The 3D page

| symptom | what to do |
| --- | --- |
| page is laggy / fans spin up | Tick **Setup -> Graphics -> Light graphics** (or open `/?lite=1`). |
| black or blank stage | Update your graphics driver; try another browser; check hardware acceleration is on in the browser settings. |
| avatar loads very slowly | `avatar.vrm` is ~16 MB; the first load after start is the slowest. |

Still stuck? Open an issue and paste the `launch.py --check` output plus the last lines of the relevant `logs/*.log`.
