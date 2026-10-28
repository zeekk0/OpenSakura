# Using any OpenAI-compatible server with OpenSakura

Besides OpenRouter and Ollama, Sakura's brain can talk to **any server that speaks the OpenAI
`/v1/chat/completions` API**. That covers `llama.cpp` (`llama-server`), LM Studio, vLLM, LocalAI,
Jan, KoboldCpp, text-generation-webui, and hosted providers such as Groq, Together, Fireworks,
DeepSeek or OpenAI itself. This is the provider called **OpenAI-compatible** in the Setup page.

## Quick start with llama.cpp

1. Start the server with a model (any chat/instruct GGUF; Qwen3 or Gemma 3 work well):

   ```bash
   llama-server -m qwen3-8b-q4_k_m.gguf --port 8080 -c 8192
   # no GPU? it runs on CPU as is. Apple Silicon / AMD / Intel GPUs: use the matching llama.cpp build.
   ```

2. Start OpenSakura and open **Setup -> LLM**.
3. In **OpenAI-compatible URL** enter `http://127.0.0.1:8080/v1` and press **Test**. The tag should
   turn green.
4. In the roles table set **provider** to **OpenAI-compatible** for the roles you want on this server
   (actor, director, action, mod). Leave the model column empty for single-model servers like
   llama.cpp, or type the model name your server lists.
5. **Save LLM**, then say hi in **Play**.

## Common servers

| server | URL to enter | key | model column |
| --- | --- | --- | --- |
| llama.cpp `llama-server` | `http://127.0.0.1:8080/v1` | none | empty |
| LM Studio (Developer -> Start Server) | `http://127.0.0.1:1234/v1` | none | the loaded model's id |
| Ollama's OpenAI endpoint | `http://127.0.0.1:11434/v1` | none | model name (but the built-in *Ollama* provider is better) |
| vLLM | `http://127.0.0.1:8000/v1` | `--api-key` value if set | the served model name |
| Groq | `https://api.groq.com/openai/v1` | your key | e.g. `llama-3.3-70b-versatile` |
| Together | `https://api.together.xyz/v1` | your key | model id |
| OpenAI | `https://api.openai.com/v1` | your key | e.g. `gpt-4o-mini` |

You can paste the URL with or without a trailing `/chat/completions`; OpenSakura strips it.

## API key

Optional. Local servers need none. If your provider needs one, paste it next to the URL and press
**Save**, or put `OPENAI_COMPAT_API_KEY=...` in `.env`. It is stored only in `.env` and sent only to the
URL you configured. (It is deliberately not called `OPENAI_API_KEY`, so a key you already have in your
environment for OpenAI is never sent to a local server by accident.)

## Optional settings (`config/settings.json`, `llm.openai`)

| key | default | meaning |
| --- | --- | --- |
| `url` | `http://127.0.0.1:8080/v1` | base URL |
| `actor_model`, `director_model`, `action_model`, `mod_model` | `""` | model per role; empty sends `default` |
| `json_mode` | `"auto"` | `auto`: ask for JSON mode, and if the server rejects it remember that and retry without; `on`: always ask; `off`: never ask (the prompts already request JSON) |
| `extra_body` | `{}` | extra fields merged into every request, e.g. `{"chat_template_kwargs": {"enable_thinking": false}}` to switch off Qwen3 thinking on llama.cpp |

## Tips and troubleshooting

- **Her replies are slow.** Reasoning models "think" before answering. Use a non-reasoning model, or turn
  thinking off with `extra_body` (above). `<think>...</think>` blocks that do come back are removed
  automatically.
- **She answers with broken JSON now and then.** Small models (under 4B) are weak at this. Use a bigger
  model, or keep the `actor` role on a stronger model and put `director`/`action`/`mod` on the small one.
- **Test stays red.** Check the server is running and the URL ends in `/v1`. A server that has no
  `/models` route still counts as online as long as it answers.
- **`401`/`403`.** The key is missing or wrong.
- **Cost.** The cost counter on the State tab shows token counts only because it cannot magically know your provider's price-per-token.
- **Privacy.** With a server on your own machine nothing leaves it. With a hosted provider your messages, her persona and relevant memory notes go to that provider under its terms.
