import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("run pip install requests beautifulsoup4")
    sys.exit(1)


SOUNDGASM_URL = "https://soundgasm.net"
SEARCH_API_URL = "https://flarusearch.com/api/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/27.0.1 Mobile/15E148 Safari/604.1"
    )
}

PAGE_DELAY = 1.0
API_DELAY = 0.2
DEFAULT_PAGE_SIZE = 20
MAX_PAGES = 2000


def split_tags(raw):
    if not raw:
        return []
    tags = []
    for piece in re.split(r"[,\n]+", raw):
        tag = piece.strip().lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def get_page(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(PAGE_DELAY * attempt * 2)


def looks_like_audio_link(url):
    if "soundgasm.net/u/" not in url and "hotaudio.net/u/" not in url:
        return False
    return url.rstrip("/").count("/") >= 5


def save_json(path, source, items):
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    payload = {"source": source, "items": items}
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def load_json(path):
    file = Path(path)
    if not file.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    return json.loads(file.read_text(encoding="utf-8"))


def parse_profile(html, profile_url):
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not (href.startswith("/u/") or href.startswith(SOUNDGASM_URL + "/u/")):
            continue

        full_url = urljoin(profile_url, href)
        if full_url.rstrip("/") == profile_url.rstrip("/"):
            continue

        title = link.get_text(strip=True)
        if title:
            posts.append({"title": title, "url": full_url, "tags": []})

    text = soup.get_text(" ", strip=True)
    page_tags = sorted({t.lower() for t in re.findall(r"#([A-Za-z0-9_-]+)", text)})
    for post in posts:
        post["tags"] = page_tags

    return posts


def scrape_profile(profile_url, output_path):
    print(f"Scraping {profile_url}")
    posts = parse_profile(get_page(profile_url), profile_url)

    unique = list({post["url"]: post for post in posts}.values())

    print(f"Found {len(unique)} audio links")
    save_json(output_path, profile_url, unique)
    print(f"Saved to {output_path}")
    return unique


def find_entry_tags(link):
    node = link
    for _ in range(6):
        node = node.parent
        if node is None:
            break

        post_links = 0
        for a in node.find_all("a", href=True):
            href = a["href"]
            absolute = urljoin("https://x/", href) if href.startswith("/") else href
            if looks_like_audio_link(absolute):
                post_links += 1
        if post_links > 1:
            break

        tags = set()
        for a in node.find_all("a", href=True):
            match = re.search(r"/k/([^/?#]+)/?$", a["href"])
            if match:
                tags.add(unquote(match.group(1)).lower())
        if tags:
            return tags

    return set()


def scrape_one_tag(tag, found, max_pages):
    base_url = f"https://searchsoundgasm.net/k/{quote(tag)}/"
    last_page = max_pages if max_pages > 0 else MAX_PAGES

    for page in range(1, last_page + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        print(f"[{tag}] page {page}: {url}")

        try:
            html = get_page(url)
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code == 404:
                print(f"  no page for tag '{tag}' on the site (404)")
            else:
                print(f"  stopping: {err}")
            break
        except Exception as err:
            print(f"  stopping: {err}")
            break

        soup = BeautifulSoup(html, "html.parser")
        links_on_page = 0
        new_on_page = 0

        for link in soup.find_all("a", href=True):
            full_url = urljoin(url, link["href"])
            if not looks_like_audio_link(full_url):
                continue

            links_on_page += 1
            tags_here = find_entry_tags(link) | {tag}

            if full_url in found:
                found[full_url]["tags"] = sorted(set(found[full_url]["tags"]) | tags_here)
                continue

            title = link.get_text(strip=True) or full_url.rstrip("/").split("/")[-1]
            found[full_url] = {"title": title, "url": full_url, "tags": sorted(tags_here)}
            new_on_page += 1

        print(f"  {links_on_page} links on page, {new_on_page} new (total {len(found)})")

        if links_on_page == 0 or new_on_page == 0:
            break
        time.sleep(PAGE_DELAY)


def merge_with_existing(items, output_path):
    existing = Path(output_path)
    try:
        old_items = json.loads(existing.read_text(encoding="utf-8")).get("items", [])
    except (json.JSONDecodeError, OSError):
        old_items = []

    merged = {item["url"]: item for item in old_items}
    for item in items:
        if item["url"] in merged:
            old_tags = set(merged[item["url"]].get("tags", []))
            merged[item["url"]]["tags"] = sorted(old_tags | set(item["tags"]))
        else:
            merged[item["url"]] = item
    return list(merged.values())


def scrape_tags(raw_tags, output_path, max_pages=0, match_any=False, fresh=False):
    tags = split_tags(raw_tags)
    if not tags:
        print("No tags given")
        sys.exit(1)

    found = {}
    for tag in tags:
        scrape_one_tag(tag, found, max_pages)

    items = list(found.values())

    if not match_any:
        wanted = set(tags)
        items = [item for item in items if wanted.issubset(item["tags"])]
        print(f"{len(items)} link(s) have all of: {', '.join(tags)}")

    if Path(output_path).exists() and not fresh:
        items = merge_with_existing(items, output_path)
        print(f"Merged with existing {output_path}")

    save_json(output_path, "tags:" + ",".join(tags), items)
    print(f"Saved {len(items)} links to {output_path}")


def post_json(url, payload, session=None, retries=3):
    post = session.post if session else requests.post
    for attempt in range(1, retries + 1):
        try:
            response = post(url, json=payload, headers=HEADERS, timeout=30)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == retries:
                raise
            time.sleep(0.5 * attempt)


def hit_matches(hit, terms):
    text = " ".join([
        str(hit.get("title") or ""),
        str(hit.get("title_clean") or ""),
        " ".join(hit.get("tags") or []),
    ]).lower()
    squashed_text = re.sub(r"[^a-z0-9]+", "", text)

    for term in terms:
        squashed_term = re.sub(r"[^a-z0-9]+", "", term.lower())
        if term.lower() not in text and squashed_term not in squashed_text:
            return False
    return True


def search_online(tags, query_text="", max_pages=0, loose=False, endpoint=SEARCH_API_URL,
                  on_page=None, workers=3, per_page=100):
    terms = list(tags)
    if query_text:
        terms.append(query_text)
    query = " ".join(terms)

    found = {}
    session = requests.Session()

    def fetch_page(offset, limit):
        data = post_json(endpoint, {"q": query, "limit": limit, "offset": offset}, session=session)
        time.sleep(API_DELAY)
        return data

    def keep_hits(data):
        for hit in data.get("hits") or []:
            url = hit.get("url")
            if not url or url in found:
                continue
            if not loose and not hit_matches(hit, terms):
                continue
            found[url] = {
                "title": hit.get("title") or hit.get("title_clean") or "Untitled",
                "url": url,
                "tags": [str(t).lower() for t in (hit.get("tags") or [])],
                "creator": hit.get("username") or "",
                "source": hit.get("source") or "",
            }
        if on_page:
            on_page(list(found.values()))

    try:
        try:
            first = fetch_page(0, per_page)
        except Exception:
            if per_page == DEFAULT_PAGE_SIZE:
                raise
            per_page = DEFAULT_PAGE_SIZE
            first = fetch_page(0, per_page)
    except KeyboardInterrupt:
        return list(found.values())
    except Exception as err:
        print(f"stopping: {err}", file=sys.stderr)
        return list(found.values())

    hits = first.get("hits") or []
    total = first.get("estimatedTotalHits") or len(hits)
    keep_hits(first)
    page_size = len(hits)
    print(f"page 1: {page_size} hits, ~{total} total, {len(found)} kept", file=sys.stderr)
    if not hits:
        return list(found.values())

    offsets = list(range(page_size, total, page_size))
    page_cap = max_pages - 1 if max_pages > 0 else MAX_PAGES
    offsets = offsets[:page_cap]
    if not offsets:
        return list(found.values())

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {pool.submit(fetch_page, offset, page_size): offset for offset in offsets}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                keep_hits(future.result())
            except Exception as err:
                print(f"  offset {futures[future]} failed: {err}", file=sys.stderr)
                continue
            print(f"  fetched {done + 1}/{len(offsets) + 1} pages, {len(found)} kept", file=sys.stderr)
    except KeyboardInterrupt:
        print("  interrupted, keeping what was found so far", file=sys.stderr)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return list(found.values())


def search_local(data, query, tags=None, match_any=False):
    query = (query or "").lower().strip()
    wanted = set(split_tags(",".join(tags))) if tags else set()
    results = []

    for item in data.get("items", []):
        title = item.get("title", "").lower()
        url = item.get("url", "").lower()
        item_tags = {t.lower() for t in item.get("tags", [])}

        if query and query not in title and query not in url:
            continue
        if wanted:
            if match_any and not (wanted & item_tags):
                continue
            if not match_any and not wanted.issubset(item_tags):
                continue
        results.append(item)

    return results


def run_scrape(args):
    scrape_profile(args.url, args.output)


def run_scrape_tag(args):
    scrape_tags(", ".join(args.tags), args.output,
                max_pages=args.pages, match_any=args.any, fresh=args.fresh)


def run_search(args):
    tags = split_tags(",".join(args.tags or []))

    if args.file:
        results = search_local(load_json(args.file), args.query, tags, match_any=args.any)
    else:
        if not tags and not args.query:
            print("Give a query or --tags")
            sys.exit(1)

        args.output = args.output or "search_results.json"
        print(f"Saving live to {args.output} (Ctrl+C any time)", file=sys.stderr)
        results = search_online(
            tags,
            args.query,
            max_pages=args.pages,
            loose=args.loose,
            endpoint=args.endpoint,
            workers=args.workers,
            per_page=args.per_page,
            on_page=lambda items: save_json(args.output, "search", items),
        )

    print(f"{len(results)} result(s):")
    for result in results:
        print(f"- {result['title']}")
        print(f"  {result['url']}")
        if result.get("tags"):
            print(f"  tags: {', '.join(result['tags'])}")

    if args.output:
        save_json(args.output, "search", results)
        print(f"Saved to {args.output}")


def build_parser():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="cmd", required=True)

    scrape = commands.add_parser("scrape")
    scrape.add_argument("url")
    scrape.add_argument("--output", "-o", default="soundgasm_links.json")
    scrape.set_defaults(func=run_scrape)

    scrape_tag = commands.add_parser("scrape-tag")
    scrape_tag.add_argument("tags", nargs="+")
    scrape_tag.add_argument("--output", "-o", default="soundgasm_links.json")
    scrape_tag.add_argument("--pages", type=int, default=0)
    scrape_tag.add_argument("--any", action="store_true")
    scrape_tag.add_argument("--fresh", action="store_true")
    scrape_tag.set_defaults(func=run_scrape_tag)

    search = commands.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--tags", nargs="+")
    search.add_argument("--any", action="store_true")
    search.add_argument("--file", "-f")
    search.add_argument("--pages", type=int, default=0)
    search.add_argument("--output", "-o")
    search.add_argument("--workers", type=int, default=3)
    search.add_argument("--per-page", type=int, default=100)
    search.add_argument("--loose", action="store_true")
    search.add_argument("--endpoint", default=SEARCH_API_URL)
    search.set_defaults(func=run_search)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
