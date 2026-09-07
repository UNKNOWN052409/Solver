import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ghostrise.wire import GhostWire

w = GhostWire(headless=True)
w.launch()
w.goto('about:blank', timeout=25000)
time.sleep(1)
print('sync readyState:', repr(w.evaluate('document.readyState')))

# direct CDP send with awaitPromise true
sid = w._sid
r = w._send("Runtime.evaluate", {
    "expression": "async () => (await fetch('https://api.ipify.org')).text()",
    "returnByValue": True, "awaitPromise": True,
}, session_id=sid)
print('direct eval result:', r)

# expression form (IIFE) instead of bare arrow
expr = ("(async () => { try { const t = await (await fetch('https://api.ipify.org')).text(); "
        "return 'IP:'+t; } catch(e) { return 'ERR:'+e.message; } })()")
r2 = w._send("Runtime.evaluate", {
    "expression": expr, "returnByValue": True, "awaitPromise": True,
}, session_id=sid)
print('iife result:', r2)
w.close()
