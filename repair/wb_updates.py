"""Anonymous release notifications; never install or replace a running EXE."""
import json
import re
import urllib.request

VERSION = "1.1.0"
RELEASE_BASE = "__RELEASE_BASE__"
UPDATE_FEED = "__UPDATE_FEED__"


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("invalid version")
    return tuple(int(part) for part in value.split("."))


def check_update(current=VERSION, feed=UPDATE_FEED, release_base=RELEASE_BASE):
    try:
        request = urllib.request.Request(feed, headers={"User-Agent": "WorkBuddy-Migration-Assistant/" + current, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read(65537).decode("utf-8"))
        remote = version_tuple(data["version"])
        url = data["url"]
        if url not in (release_base + "/releases/latest", release_base + "/releases/tag/v" + data["version"]):
            raise ValueError("invalid release URL")
        return {"ok": True, "available": remote > version_tuple(current), "current": current,
                "version": data["version"], "url": url, "notes": str(data.get("notes") or "")[:2000]}
    except Exception:
        return {"ok": False, "available": False, "current": current, "msg": "暂时无法检查更新，请稍后重试。"}


def install(web):
    original = web["Handler"]._do_get

    def do_get(self):
        if self.path.split("?", 1)[0] == "/api/update":
            return self._json(check_update())
        return original(self)

    web["Handler"]._do_get = do_get
