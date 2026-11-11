"""Outbound HTTP(S) through the proxy the service environment names.

No proxy address lives in this repository.  The appliance runs on whatever
network its owner has: some reach GitHub directly, some only through a LAN
proxy.  The service therefore reads the usual environment variables and, when
none of them is set, every request goes out directly — an untouched install
behaves exactly as it did before.

Only explicit internet downloads use :func:`opener` (release check and assets,
plugin upstream index and builds).  :func:`seal_direct` pins the process-wide
default ``urlopen`` opener to a direct connection, which keeps the console
file managers, the NAS, the local payload repository and the health checks off
the proxy even when the environment sets one for the whole service.
"""
import os
import urllib.parse
import urllib.request

#: Read in this order; the first non-empty value wins.  ``PS5JB_PROXY`` is ours
#: and lets one service be pointed at a proxy without touching anything else;
#: the remaining names are the de-facto standard ones, in both spellings
#: because ``urllib`` only honours the lower-case form.
VARIABLES = ('PS5JB_PROXY', 'HTTPS_PROXY', 'https_proxy',
             'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy')
#: One address serves both schemes: GitHub and the plugin mirror are HTTPS, and
#: a plain HTTP proxy tunnels those with CONNECT.
SCHEMES = ('http', 'https')


def lookup(environ=None):
    """Return ``(url, variable, error)`` for the proxy the environment asks for.

    A bare ``host:port`` gains an ``http://`` scheme, which is how most people
    write a LAN proxy.  ``error`` explains a value that cannot be used, such as
    a SOCKS URL: ``urllib`` has no SOCKS transport, so pretending otherwise
    would only hide the cause of a later timeout.
    """
    env = os.environ if environ is None else environ
    for name in VARIABLES:
        raw = (env.get(name) or '').strip()
        if not raw:
            continue
        url = raw if '://' in raw else 'http://' + raw
        try:
            parsed = urllib.parse.urlsplit(url)
            usable = parsed.scheme in SCHEMES and bool(parsed.hostname)
        except ValueError:
            usable = False
        if usable:
            return url, name, ''
        return '', name, f'{name} 不是可用的 HTTP 代理（只支持 http:// 或 https://，不支持 SOCKS）'
    return '', '', ''


def proxies(environ=None):
    """Mapping for ``urllib``'s ProxyHandler; empty means "connect directly"."""
    url, _, error = lookup(environ)
    return {} if error or not url else {'http': url, 'https': url}


def opener(redirect=None, environ=None):
    """Opener for internet fetches: the environment proxy, or a direct link."""
    handlers = [urllib.request.ProxyHandler(proxies(environ))]
    if redirect is not None:
        handlers.append(redirect())
    return urllib.request.build_opener(*handlers)


def masked(url):
    """The address with any credentials removed, for the admin page."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = f':{parsed.port}' if parsed.port else ''
    except ValueError:
        return '格式不正确'
    if not parsed.hostname:
        return '格式不正确'
    credentials = '***@' if parsed.username or parsed.password else ''
    return f'{parsed.scheme}://{credentials}{parsed.hostname}{port}'


def status(environ=None):
    """What the admin page shows: direct, proxied, or a rejected setting."""
    url, variable, error = lookup(environ)
    return {'configured': bool(url) and not error, 'url': masked(url) if url else '',
            'variable': variable, 'error': error}


def hint(environ=None):
    """Advice for a failed internet fetch on a network that cannot reach out.

    Appended to the error the admin page already shows, so the next step is
    readable where the failure appears instead of in a log file.
    """
    state = status(environ)
    if state['error']:
        return f'；代理配置不可用：{state["error"]}'
    if not state['configured']:
        return '；若本机无法直连外网，可给服务设置 HTTPS_PROXY 代理后重启'
    return ''


def seal_direct():
    """Pin the default ``urlopen`` opener to a direct connection.

    The service runs with one environment for everything it does, so a proxy
    set for GitHub would otherwise also sit in front of the PS5, the NAS and
    the local repository.  Internet fetches build their own opener instead.
    """
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({})))
