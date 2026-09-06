{
  "_meta": {
    "probe_date": "2026-09-06",
    "engine": "GhostWire raw-CDP on manual chromium 9227",
    "token_source": "Rev/config.json Bearer (26-Aug)",
    "note": "Qwen Studio SPA — sab routes '/' pe redirect (client-side routing). Image/video-gen endpoints GUEST mode me nahi dikhte; model-select (Qwen-Image/Wan) + MCP/plugin attach pe trigger hote hain. History server-side (localStorage chat-keys 0)."
  },
  "base": "https://chat.qwen.ai",
  "api_surface_captured": [
    "POST /api/v1/auths/",
    "GET  /api/v2/configs/",
    "GET  /api/v2/configs/setting-config",
    "GET  /api/v2/models/",
    "GET  /api/v2/tts/config?omni_speakers=v1&audio_tts_speakers=v1&omni_language=v1&audio_tts_language=v1",
    "POST /api/v2/users/status"
  ],
  "video_gen_endpoints": "NOT_YET_CAPTURED (guest mode hides them; need logged-in model-select /api/v2/chat/completions with chat_type=image or video)",
  "image_gen_endpoints": "NOT_YET_CAPTURED (same gate)",
  "history_clear": "localStorage chat-conversation keys = 0 found; history is server-side (account conversations), needs DELETE /api/v2/conversations/{id} or per-conversation clear in logged-in UI",
  "anti_captcha": {
    "RGV587_ERROR": "Alibaba WAF punish (滑块/slider). Direct-fetch blocked (aliyun_waf_aa challenge HTML). Browser-context fetch + cookies + token = REQUIRED. Verified: page-context fetch returns 200/real, direct urllib returns WAF HTML."
  }
}
