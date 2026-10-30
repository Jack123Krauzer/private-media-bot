"""OmniDL-derived Instagram adapter.
Original implementation: https://github.com/DzarelDeveloper/OmniDL
Preserves Instagram image sources returned by yt-dlp. Does NOT bypass login.
"""
from urllib.parse import urlparse
from yt_dlp.extractor.instagram import InstagramIE


class OmniInstagramIE(InstagramIE):
    def _extract_product_media(self, media):
        result = super()._extract_product_media(media)
        if media.get("media_type") == 1:
            candidates = [
                candidate for candidate in
                media.get("image_versions2", {}).get("candidates", [])
                if candidate.get("url")
            ]
            if not candidates:
                raise ValueError("No image source URL returned by Instagram")
            best = max(
                candidates,
                key=lambda item: (item.get("width") or 0) * (item.get("height") or 0)
            )
            ext = urlparse(best["url"]).path.rsplit(".", 1)[-1].lower()
            if ext not in {"jpg", "jpeg", "png", "webp", "avif"}:
                ext = "jpg"
            result.update(
                url=best["url"],
                ext=ext,
                thumbnail=best["url"],
                formats=[{"url": best["url"], "ext": ext}],
            )
        else:
            for fmt in result.get("formats", []):
                fmt.setdefault("ext", "mp4")
                fmt["vcodec"] = fmt.get("vcodec") or "unknown"
                fmt["acodec"] = fmt.get("acodec") or "unknown"
        return result
