"""GitHub via its public API: real, structured profile and repo data (no scraping)."""

import base64

import httpx

from .sources import client, parse_date

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json"}


async def github(user: str, repo: str | None = None) -> dict:
    """→ {url, site, title, text, published}. Raises httpx.HTTPError on network failure."""
    async with client() as http:
        return await (_repo(http, user, repo) if repo else _profile(http, user))


async def _profile(http: httpx.AsyncClient, user: str) -> dict:
    r = await http.get(f"{API}/users/{user}", headers=HEADERS)
    if r.status_code == 404:
        return {"url": f"https://github.com/{user}", "site": "github.com", "title": f"GitHub: {user}",
                "text": f"There is no GitHub account with the username '{user}'.", "published": None}
    r.raise_for_status()
    u = r.json()
    repos = (await http.get(f"{API}/users/{user}/repos", headers=HEADERS,
                            params={"sort": "updated", "per_page": 12})).json()
    lines = [
        f"GitHub profile of {u.get('name') or u['login']} (username {u['login']}): {u['html_url']}",
        f"Bio: {u.get('bio') or '-'}",
        f"Location: {u.get('location') or '-'} · Company: {u.get('company') or '-'} · Website: {u.get('blog') or '-'}",
        f"Followers {u.get('followers', 0)} · Following {u.get('following', 0)} · Public repositories "
        f"{u.get('public_repos', 0)} · Joined {u.get('created_at', '')[:10]}",
        "Most recently updated repositories:",
    ]
    latest = None
    for repo in repos if isinstance(repos, list) else []:
        pushed = parse_date(repo.get("pushed_at"))
        latest = max(filter(None, [latest, pushed]), default=None)
        lines.append(
            f"- {repo['name']} ({repo.get('language') or 'no language'}, {repo.get('stargazers_count', 0)} stars, "
            f"{'fork, ' if repo.get('fork') else ''}last push {repo.get('pushed_at', '')[:10]}): "
            f"{repo.get('description') or 'no description'}"
        )
    return {"url": u["html_url"], "site": "github.com", "title": f"{u.get('name') or u['login']} on GitHub",
            "text": "\n".join(lines), "published": latest}


async def _repo(http: httpx.AsyncClient, user: str, repo: str) -> dict:
    r = await http.get(f"{API}/repos/{user}/{repo}", headers=HEADERS)
    if r.status_code == 404:
        return {"url": f"https://github.com/{user}/{repo}", "site": "github.com", "title": f"{user}/{repo}",
                "text": f"No public repository {user}/{repo} found.", "published": None}
    r.raise_for_status()
    g = r.json()
    lines = [
        f"Repository {g['full_name']}: {g['html_url']}",
        f"Description: {g.get('description') or '-'}",
        f"Language {g.get('language') or '-'} · {g.get('stargazers_count', 0)} stars · {g.get('forks_count', 0)} forks"
        f" · open issues {g.get('open_issues_count', 0)} · created {g.get('created_at', '')[:10]}"
        f" · last push {g.get('pushed_at', '')[:10]}",
        f"Topics: {', '.join(g.get('topics') or []) or '-'}",
    ]
    readme = await http.get(f"{API}/repos/{user}/{repo}/readme", headers=HEADERS)
    if readme.status_code == 200 and readme.json().get("content"):
        text = base64.b64decode(readme.json()["content"]).decode("utf-8", errors="ignore")
        lines += ["README (start):", text[:3000]]
    return {"url": g["html_url"], "site": "github.com", "title": g["full_name"], "text": "\n".join(lines),
            "published": parse_date(g.get("pushed_at"))}
