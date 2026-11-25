import http.client
import json
import re
import time
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit


COMMONS = "commons.wikimedia.org"
IMAGE_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
LICENSE_PATTERN = re.compile(r"/(?:licenses/by(?:-sa)?/[1-4]\.\d|publicdomain/zero/1\.0)/?")
FISH = re.compile(r"лосос|семг")
BAKED = re.compile(r"духов|запеч|фольг|пергамент")
UNCOOKED = re.compile(r"солен|засол|тартар|сашими|суши|ролл|сырой")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(html):
    parser = PlainText()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def photo_url(raw, hosts):
    address = urlsplit(raw)
    if address.scheme != "https" or address.netloc not in hosts:
        return None
    return urlunsplit((address.scheme, address.netloc, address.path, "", ""))


@lru_cache(maxsize=96)
def commons_query(filename, dish, _day):
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "500",
        "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|ImageDescription",
    }
    if filename:
        params["titles"] = "File:" + filename
    else:
        params.update(generator="search", gsrnamespace=6, gsrlimit=4, gsrsearch=dish)
    connection = http.client.HTTPSConnection(COMMONS, timeout=4)
    try:
        connection.request("GET", "/w/api.php?" + urlencode(params), headers={
            "User-Agent": "RecipeIdea/0.1 (https://github.com/K3RZH/VK-recipe-bot)"
        })
        response = connection.getresponse()
        if response.status != 200:
            return []
        return json.loads(response.read(300_000)).get("query", {}).get("pages", [])
    finally:
        connection.close()


def photo_search(title):
    name = title.casefold().replace("ё", "е")
    if FISH.search(name) and not UNCOOKED.search(name) and BAKED.search(name):
        return "Salmon baked in an oven with lemons.jpg", "Запечённый лосось"
    if ("макарон" in name and "тушенк" in name) or "по-флотски" in name:
        return "Navy-style.jpg", "Макароны по-флотски"
    return "", title


def find_photo(recipe):
    title = recipe["title"]
    filename, label = photo_search(title)
    keywords = set(re.findall(r"[а-яa-z]{4,}", title.casefold().replace("ё", "е")))
    if not filename and not keywords:
        return None
    try:
        pages = commons_query(filename, "" if filename else title[:100], int(time.time() // 86_400))
        for page in pages:
            info = next(iter(page.get("imageinfo", [])), {})
            if info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            metadata = info.get("extmetadata", {})
            description = plain_text(metadata.get("ImageDescription", {}).get("value", ""))
            if not filename:
                picture = (page.get("title", "") + " " + description).casefold().replace("ё", "е")
                words = set(re.findall(r"[а-яa-z]+", picture))
                if len(keywords) < 2 or not keywords <= words:
                    continue
            source = photo_url(info.get("descriptionurl", ""), {COMMONS})
            image = photo_url(info.get("thumburl") or info.get("url", ""), IMAGE_HOSTS)
            license_url = photo_url(
                metadata.get("LicenseUrl", {}).get("value", ""), {"creativecommons.org"}
            )
            author = plain_text(metadata.get("Artist", {}).get("value", ""))
            license_name = plain_text(metadata.get("LicenseShortName", {}).get("value", ""))
            if not source or not image or not license_url or not author or not license_name:
                continue
            if not LICENSE_PATTERN.fullmatch(urlsplit(license_url).path):
                continue
            return {
                "src": image, "page": source, "title": label, "author": author[:160],
                "license": license_name, "license_url": license_url,
                "width": info.get("thumbwidth") or info.get("width"),
                "height": info.get("thumbheight") or info.get("height"),
            }
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        return None
    return None


def find_photos(recipes):
    photos = {}
    for recipe in recipes:
        photo = find_photo(recipe)
        if photo:
            photos[photo["src"]] = photo
    return list(photos.values())
