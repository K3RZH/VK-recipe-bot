import http.client
import ipaddress
import json
import os
import re
import socket
from functools import lru_cache
from urllib.parse import quote, urlencode, urlsplit
from urllib.robotparser import RobotFileParser


AGENT = "RecipeIdea/0.1 (educational recipe search)"
NETWORK_ERRORS = (OSError, http.client.HTTPException)


def discover(ingredients, prefer_cooked, pantry):
    names = {item.casefold() for item in ingredients}
    terms = sorted(item for item in names if re.fullmatch(r"[а-яё]{2,24}(?:[ -][а-яё]{1,24}){0,7}", item))
    if not terms:
        return []
    suffix = "из продуктов" if pantry else "приготовление" if prefer_cooked else ""
    query = " ".join(filter(None, ["рецепт", *terms[:6], suffix]))
    if len(query) > 240:
        return []
    provider = urlsplit(os.getenv("RECIPE_SEARX_URL", "http://127.0.0.1:8888"))
    if provider.scheme not in {"http", "https"} or not provider.hostname:
        raise ValueError("Укажи HTTP(S)-адрес SearXNG в RECIPE_SEARX_URL.")
    connection_type = http.client.HTTPSConnection if provider.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(provider.hostname, provider.port, timeout=7)
    params = urlencode({"q": query, "format": "json", "language": "ru-RU", "categories": "general"})
    try:
        connection.request("GET", provider.path.rstrip("/") + "/search?" + params,
                           headers={"Accept": "application/json", "User-Agent": AGENT})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"SearXNG: HTTP {response.status}")
        result = json.loads(read_limited(response, 1_000_000))
    finally:
        connection.close()
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise ValueError("Некорректный ответ SearXNG.")
    if not result["results"] and result.get("unresponsive_engines"):
        raise OSError("Поисковые системы SearXNG не ответили.")
    return [item["url"] for item in result["results"][:30]
            if isinstance(item, dict) and isinstance(item.get("url"), str)]


def read_limited(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Ответ больше лимита.")
    return data


def recipe_address(url):
    if len(url) > 2000 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("Недопустимый адрес рецепта.")
    address = urlsplit(url)
    if (address.scheme != "https" or not address.hostname or address.username is not None
            or address.port not in (None, 443)):
        raise ValueError("Адрес рецепта должен использовать HTTPS без учётных данных.")
    host = address.hostname.encode("idna").decode("ascii").rstrip(".")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]+", host):
        raise ValueError("Адрес рецепта должен содержать доменное имя.")
    path = quote(address.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if address.query:
        path += "?" + quote(address.query, safe="%/:@!$&'()*+,;=?-._~")
    return host, path


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host):
        super().__init__(host, timeout=4)
        # Подключаемся к проверенному IP и сохраняем домен для проверки TLS.
        self._create_connection = self.connect_public

    @staticmethod
    def connect_public(address, timeout, _source_address):
        host, port = address
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or getattr(ip, "is_site_local", False) for ip in ips):
            raise ValueError("Домен ведёт в непубличную сеть.")
        for family, kind, protocol, _name, sockaddr in addresses:
            connection = socket.socket(family, kind, protocol)
            try:
                connection.settimeout(timeout)
                connection.connect(sockaddr)
                return connection
            except OSError:
                connection.close()
        raise OSError("Не удалось подключиться к публичному адресу сайта.")


def fetch(host, path, limit, media_type):
    connection = PublicHTTPSConnection(host)
    try:
        connection.request("GET", path, headers={"User-Agent": AGENT, "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200:
            return response.status, ""
        if response.headers.get_content_type() not in media_type:
            raise ValueError("Неожиданный тип страницы.")
        charset = response.headers.get_content_charset() or "utf-8-sig"
        return response.status, read_limited(response, limit).decode(charset, errors="replace")
    finally:
        connection.close()


@lru_cache(maxsize=256)
def robots(host, _period):
    status, content = fetch(host, "/robots.txt", 100_000, {"text/plain", "text/html"})
    if status not in {200, 404, 410}:
        raise OSError(f"robots.txt: HTTP {status}")
    rules = RobotFileParser()
    rules.parse(content.splitlines())
    return rules


@lru_cache(maxsize=128)
def recipe_page(host, path, period):
    rules = robots(host, period)
    if (not rules.can_fetch(AGENT, f"https://{host}{path}") or rules.crawl_delay(AGENT)
            or rules.request_rate(AGENT)):
        return ""
    _status, content = fetch(host, path, 1_500_000, {"text/html", "application/xhtml+xml"})
    return content
