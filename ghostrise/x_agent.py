"""X keyless stack for GhostRise — timeline, search, single post.

Rust GhostMouse ka X chain (syndication/RSS) timeline ke liye perfect
hai, par SEARCH pe nitter instances bot-wall ("Verifying your browser")
lage hote hain jo sirf real JS engine clear karta hai. Ye module wahi
part GhostRise (CloakBrowser/playwright) se karta hai — wall clear +
search + parse, keyless.

Usage (inside GhostSession):
    with GhostSession(profile="x") as g:
        from ghostrise.x_agent import x_search, x_posts, x_post
        posts = x_search(g, "ghostmouse browser", limit=10)
        tl    = x_posts(g, "elonmusk", limit=10)
"""
import re
import time


WALL_MARKERS = ("Verifying your browser", "Checking your browser",
                "Just a moment", "Enable JavaScript and cookies",
                "Checking browser integrity")


def _page(g, url, wait=6):
    page = g.page(url)
    time.sleep(wait)
    return page


def _clear_wall(page, max_wait=30):
    """JS bot-wall interstitials real browser me khud clear hote hain —
    bas wait + reload loop."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            body = page.eval_on_selector("body", "e => e.innerText.slice(0, 400)")
        except Exception:
            body = ""
        if not any(m in body for m in WALL_MARKERS):
            return True
        time.sleep(2.5)
    # ek reload try
    try:
        page.reload(wait_until="domcontentloaded")
        time.sleep(5)
        body = page.eval_on_selector("body", "e => e.innerText.slice(0, 400)")
        return not any(m in body for m in WALL_MARKERS)
    except Exception:
        return False


def _parse_items(page, limit):
    """nitter-style timeline items -> [{id, text, author, created, link}]."""
    try:
        raw = page.eval_on_selector_all(
            ".timeline-item",
            """els => els.slice(0, %d).map(it => {
                const link = it.querySelector('.tweet-link');
                const content = it.querySelector('.tweet-content');
                const name = it.querySelector('.fullname');
                const date = it.querySelector('.tweet-date');
                return {
                    id: link ? (link.getAttribute('href') || '').split('/').pop() : '',
                    text: content ? content.innerText.trim() : '',
                    author: name ? name.innerText.trim() : '',
                    created: date ? (date.getAttribute('title') || '') : '',
                    link: link ? link.getAttribute('href') : '',
                };
            })""" % limit)
        return [t for t in raw if t.get("text")]
    except Exception:
        return []


def x_search(g, query, limit=10, profile=None):
    """Keyless X search — nitter search pe wall clear karke parse.

    NOTE (honest, Sep 2026): public nitter mirrors ne SEARCH pe
    "automated verification" wall lagaya hai jo CloakBrowser aur
    playwright dono pe block kar deta hai (timeline RSS abhi bhi open
    hai). Search-mirror khul jaye to ye turant kaam karega — parser
    + wall-clear ready hai.
    """
    import urllib.parse
    q = urllib.parse.quote_plus(query)
    last_err = "no instances tried"
    for base in ("https://xcancel.com", "https://nitter.net",
                 "https://nitter.poast.org", "https://lightbrd.com"):
        url = f"{base}/search?f=tweets&q={q}"
        try:
            page = _page(g, url)
            if not _clear_wall(page):
                last_err = f"{base}: wall not cleared"
                continue
            items = _parse_items(page, limit)
            if items:
                return items
            last_err = f"{base}: no timeline items"
        except Exception as e:
            last_err = f"{base}: {str(e)[:80]}"
    raise RuntimeError(
        f"X search failed: {last_err} — "
        "public nitter search walls (Sep 2026); x_posts timeline chain "
        "ya SearXNG web-search fallback use karo")


def x_posts(g, handle, limit=10):
    """Timeline via nitter (wall-cleared browser) — jab Rust syndication
    chain na chale (IP block etc.) ye browser fallback hai."""
    last_err = "no instances tried"
    for base in ("https://nitter.net", "https://xcancel.com",
                 "https://lightbrd.com"):
        url = f"{base}/{handle}"
        try:
            page = _page(g, url)
            if not _clear_wall(page):
                last_err = f"{base}: wall not cleared"
                continue
            items = _parse_items(page, limit)
            if items:
                return items
            last_err = f"{base}: no timeline items"
        except Exception as e:
            last_err = f"{base}: {str(e)[:80]}"
    raise RuntimeError(f"X posts failed: {last_err}")


def x_post(g, post_id, base="https://xcancel.com"):
    """Single post page se poora tweet."""
    url = f"{base}/status/{post_id}"
    page = _page(g, url)
    if not _clear_wall(page):
        raise RuntimeError("wall not cleared")
    try:
        raw = page.eval_on_selector(
            ".timeline-item, .conversation .main-tweet, .tweet-content",
            """el => {
                const it = el.closest('.timeline-item') || el;
                const link = it.querySelector('.tweet-link') || {getAttribute: () => location.pathname};
                const content = it.querySelector('.tweet-content');
                const name = it.querySelector('.fullname');
                const date = it.querySelector('.tweet-date');
                return {
                    id: (link.getAttribute('href') || '').split('/').pop(),
                    text: content ? content.innerText.trim() : '',
                    author: name ? name.innerText.trim() : '',
                    created: date ? (date.getAttribute('title') || '') : '',
                };
            }""")
        return raw
    except Exception as e:
        raise RuntimeError(f"parse failed: {e}")
