# Qwen Integration (Live)
Endpoint: http://127.0.0.1:8050 (qwen_browser_bridge / qwen_bridge)
Alias: qwen -> qwen-max (Hermes config.yaml custom provider)
Token source: /home/kali/Rev/config.json (Bearer 209-char)
Bridge surfaces: /v1/chat, /v1/image, /v1/video, /v1/code, /v1/models, /auth/oauth, /token
Anti-captcha: browser-context fetch (aliyun_waf_aa / RGV587 = WAF gate real, direct-fetch block)
History-clear: server-side (/api/v2/conversations/ delete best-effort) + localStorage clear
Image/video-gen: GUEST mode hidden; real endpoint trigger requires logged-in model-select (UI route)
Bot-connect: qwen_bot.py uses BOT_TOKEN 8991246451 -> bridge /v1/chat/completions
Colab GPU: session ghostrise-tilenet ready (quota-throttled by Colab 412/503)
