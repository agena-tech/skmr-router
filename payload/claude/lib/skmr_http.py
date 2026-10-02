"""Authenticated agent requests must never forward credentials via redirects."""
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, 502, 'authenticated redirect denied', headers, fp)


def http_open(request, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
