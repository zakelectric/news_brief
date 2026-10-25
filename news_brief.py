"""Daily cross-outlet news brief: pull RSS from several outlets, group the biggest stories,
summarize each, and flag where the outlets disagree.

    python news_brief.py run [--date YYYY-MM-DD]   # fetch + analyze + render
    python news_brief.py render [--date ...]       # re-render HTML from saved JSON
    python news_brief.py overview [--date ...]     # rewrite only the top brief, then re-render
    python news_brief.py reanalyze [--date ...]    # refetch saved stories' articles, redo analysis + brief
    python news_brief.py publish                   # copy pages to integrated-dynamics.net/news_brief + deploy

LLM work goes through headless `claude -p` (billed to the Claude Max login, not an API key).
Output lands in site/ (served by the news_brief_web tmux session) and is published to
https://integrated-dynamics.net/news_brief/ by `publish`.
"""
import argparse
import html
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
import trafilatura

ROOT = Path(__file__).resolve().parent
SITE = ROOT / 'site'
DATA = SITE / 'data'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/130 Safari/537.36'}
MODEL = 'claude-opus-5-5'
WINDOW_HOURS = 30   # how far back an item can be published and still count for today's brief
TOP_STORIES = 12
PER_SOURCE_PER_STORY = 2
ARTICLE_CHARS = 9000

SOURCES = {
    'CBS News': ['https://www.cbsnews.com/latest/rss/main', 'https://www.cbsnews.com/latest/rss/us',
                 'https://www.cbsnews.com/latest/rss/world', 'https://www.cbsnews.com/latest/rss/politics'],
    'Fox News': ['https://moxie.foxnews.com/google-publisher/latest.xml',
                 'https://moxie.foxnews.com/google-publisher/us.xml',
                 'https://moxie.foxnews.com/google-publisher/world.xml',
                 'https://moxie.foxnews.com/google-publisher/politics.xml'],
    'Al Jazeera': ['https://www.aljazeera.com/xml/rss/all.xml'],
    'CNBC': ['https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114',
             'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362',
             'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000113'],
    'BBC': ['https://feeds.bbci.co.uk/news/rss.xml', 'https://feeds.bbci.co.uk/news/world/rss.xml'],
    'NPR': ['https://feeds.npr.org/1001/rss.xml', 'https://feeds.npr.org/1004/rss.xml',
            'https://feeds.npr.org/1014/rss.xml'],
    # AP has no public RSS and its site sits behind a Cloudflare challenge that blocks plain requests,
    # so these are hub pages read in headless Chromium (see browser_get).
    'AP': ['https://apnews.com/hub/ap-top-news', 'https://apnews.com/world-news',
           'https://apnews.com/us-news', 'https://apnews.com/politics'],
}
BROWSER_SOURCES = {'AP'}
AP_PER_HUB = 30   # hub pages run long into older features; the top of each is today's news
SOURCE_COLORS = {'CBS News': '#2f6fde', 'Fox News': '#c8312f', 'Al Jazeera': '#c98a10',
                 'CNBC': '#1a8f6a', 'BBC': '#7a4fc9', 'NPR': '#3a7f9e', 'AP': '#c2408f'}


def log(*a):
    print(datetime.now().strftime('%H:%M:%S'), *a, flush=True)


def strip_html(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()


# ---------------------------------------------------------------- fetch

def browser_get(urls):
    """{url: html} via headless Chromium, for sites that challenge plain requests (AP).

    Each page gets a fresh browser context: the first page of a context passes the Cloudflare
    check, later ones in the same context are challenged. A page still on the challenge, or one
    that fails to load, is left out and logged.
    """
    from playwright.sync_api import sync_playwright
    pages = {}
    if not urls:
        return pages
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--disable-blink-features=AutomationControlled'])
        for url in urls:
            ctx = browser.new_context(user_agent=UA['User-Agent'])
            try:
                page = ctx.new_page()
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(1500)
                if 'Just a moment' in page.title():
                    log(f'browser challenged {url}')
                else:
                    pages[url] = page.content()
            except Exception as e:
                log(f'browser failed {url}: {e}')
            finally:
                ctx.close()
        browser.close()
    return pages


def ap_hub_entries(page, cutoff):
    """Feed-like entries from an AP hub page's story cards (title, description, timestamp when shown)."""
    from bs4 import BeautifulSoup
    out = []
    for card in BeautifulSoup(page, 'lxml').select('.PagePromo'):
        a = card.select_one('.PagePromo-title a[href^="https://apnews.com/article/"]')
        if not a:
            continue
        pub = None
        ts = card.select_one('[data-timestamp]')
        if ts and ts['data-timestamp'].isdigit():
            pub = datetime.fromtimestamp(int(ts['data-timestamp']) / 1000, timezone.utc)
            if pub < cutoff:
                continue
        desc = card.select_one('.PagePromo-description')
        out.append({'link': a['href'], 'title': a.get_text(' ', strip=True),
                    'summary': desc.get_text(' ', strip=True) if desc else '', 'pub': pub})
        if len(out) >= AP_PER_HUB:
            break
    return out


def fetch_items():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items, seen = [], set()
    for source, urls in SOURCES.items():
        n = 0
        if source in BROWSER_SOURCES:
            for e in (e for page in browser_get(urls).values() for e in ap_hub_entries(page, cutoff)):
                link = e['link'].split('?')[0]
                if link in seen or not e['title']:
                    continue
                seen.add(link)
                n += 1
                items.append({'id': f'{source.split()[0].lower()}{n}', 'source': source, 'title': e['title'],
                              'summary': e['summary'][:400], 'link': link,
                              'published': e['pub'].isoformat() if e['pub'] else '', 'feed_content': ''})
            log(f'{source}: {n} items')
            continue
        for url in urls:
            try:
                r = requests.get(url, headers=UA, timeout=25)
                feed = feedparser.parse(r.content)
            except Exception as e:
                log(f'feed failed {source} {url}: {e}')
                continue
            for e in feed.entries:
                link = (e.get('link') or '').split('?')[0]
                title = strip_html(e.get('title'))
                if not link or not title or link in seen or '/video/' in link or '/live-news/' in link:
                    continue
                pub = None
                for k in ('published', 'updated'):
                    if e.get(k):
                        try:
                            pub = parsedate_to_datetime(e[k]).astimezone(timezone.utc)
                        except Exception:
                            pass
                        break
                if pub and pub < cutoff:
                    continue
                seen.add(link)
                content = ''
                if e.get('content'):
                    content = strip_html(e.content[0].get('value', ''))
                n += 1
                items.append({'id': f'{source.split()[0].lower()}{n}', 'source': source, 'title': title,
                              'summary': strip_html(e.get('summary'))[:400], 'link': link,
                              'published': pub.isoformat() if pub else '', 'feed_content': content})
        log(f'{source}: {n} items')
    return items


def fetch_article(item):
    text = ''
    page = item.pop('page', None)   # prefetched in the browser for BROWSER_SOURCES
    try:
        if page is None and item['source'] not in BROWSER_SOURCES:
            r = requests.get(item['link'], headers=UA, timeout=25)
            page = r.text if r.ok else None
        if page:
            text = trafilatura.extract(page, include_comments=False, include_tables=False) or ''
    except Exception as e:
        log(f'article failed {item["link"]}: {e}')
    if len(text) < len(item.get('feed_content', '')):
        text = item['feed_content']
    if len(text) < 300:
        text = (item['summary'] + '\n' + text).strip()
    return text[:ARTICLE_CHARS]


# ---------------------------------------------------------------- claude

def claude(system, prompt, schema, effort='medium', timeout=1200, tries=2):
    for attempt in range(tries):
        try:
            p = subprocess.run(['claude', '-p', '--model', MODEL, '--effort', effort, '--tools', '',
                                '--no-session-persistence', '--system-prompt', system,
                                '--json-schema', json.dumps(schema), '--output-format', 'json'],
                               input=prompt, capture_output=True, text=True, timeout=timeout)
            out = json.loads(p.stdout)
            if out.get('is_error') or not out.get('structured_output'):
                raise RuntimeError(str(out.get('result'))[:300])
            return out['structured_output']
        except Exception as e:
            log(f'claude attempt {attempt + 1} failed: {type(e).__name__}: {str(e)[:300]}')
            time.sleep(10)
    raise RuntimeError('claude failed')


CLUSTER_SYSTEM = f"""You are the editor of a daily cross-outlet news brief. You get headline items from several
outlets ({', '.join(SOURCES)}) published in roughly the last day.

Pick the {TOP_STORIES} most important news stories of the day and group every item that reports on the same
specific story (same event or development, not just the same broad topic). Rank by real-world
significance and by how many different outlets covered it. Strongly prefer stories covered by 3+ outlets;
include a story covered by only one outlet only if it is clearly major news. Skip lifestyle, recipes,
celebrity gossip, deals/shopping, sports results and opinion columns unless they are the dominant story.
Each item id may appear in at most one story. Before answering, check your list for overlap: if two stories
share the same event, meeting, people or announcement (e.g. a summit and a prisoner release that happened
around it), merge them into one story. Every story in the list must be clearly distinct."""

CLUSTER_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['stories'], 'properties': {
    'stories': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['title', 'topic', 'item_ids'],
        'properties': {'title': {'type': 'string', 'description': 'neutral, plain-language story title'},
                       'topic': {'type': 'string', 'enum': ['US politics', 'World', 'Business & markets',
                                                            'Conflict', 'Science & health', 'Tech',
                                                            'Crime & justice', 'Disaster & weather', 'Other']},
                       'item_ids': {'type': 'array', 'items': {'type': 'string'}}}}}}}

ANALYZE_SYSTEM = """You compare how different news outlets report the same story. You get the full text (or the
feed summary when the article couldn't be fetched) of each outlet's article(s).

Write:
- headline: a neutral headline.
- summary: 2-4 sentences of what happened, using only facts the outlets agree on or that you attribute.
- agreed_facts: the key facts all or most outlets agree on (short bullets).
- discrepancies: every place the outlets genuinely differ. Kinds:
    factual  - conflicting facts: numbers, casualty counts, dates, names, sequence of events, who said what,
               what a bill/ruling/report actually says.
    framing  - the same facts presented with materially different emphasis, loaded wording, labels
               (e.g. "militants" vs "fighters"), headline slant, or which side gets quoted.
    omission - a significant fact or context one outlet includes that others leave out, where leaving it out
               changes the reader's impression.
  For each, give each outlet's version in its own words (short exact quote when possible) and a
  likely_explanation (e.g. "articles published hours apart; toll was updated", "different official sources",
  "editorial framing", "unclear"). Rate significance: high (a reader would come away believing something
  different), medium, low. Do not invent disagreements: if outlets simply cover different angles without
  conflict, say so in coverage_note instead. Note when a source's text was only a short feed summary, since
  missing detail there is not a real omission.
  Do NOT flag differences that are expected in a developing story and leave the reader with the same picture:
  tallies that climb as officials update them (an earlier "nearly 1,000 arrests" and a later "1,314"),
  rounding ("about 60" vs "64"), counts that differ only because they cover different times or places,
  figures from different officials or agencies (a prosecutor's 64 arrests vs a ministry's 142 for the same
  city), loose figures that overlap ("some 3,000" vs "more than 2,000"), or one outlet simply giving more
  detail. Put the range in agreed_facts instead (e.g. "Arrests rose from about
  1,000 Saturday to 1,314 by Sunday, per the interior minister"). Flag a number only when the outlets'
  figures cannot both be right, or the gap would change a reader's sense of scale. Fewer, real flags are
  better than many small ones; when the outlets agree, an empty list is the right answer.
- discrepancy_score: 1-100, how much the outlets' accounts actually conflict, judged as a whole, not by
  counting flags. 1-10: they tell the same story, any differences are wording, detail or updated figures.
  11-30: minor differences in emphasis or detail a careful reader would notice. 31-60: real framing or
  omission differences that shape the impression of the story. 61-85: conflicting facts on important
  points. 86-100: the outlets describe fundamentally different events or versions of what happened.
- score_reason: one short sentence (under 20 words) explaining the score.
- coverage_note: one or two sentences on how coverage differed overall (angle, prominence, tone).
- single_source_claims: notable claims only one outlet reports (attributed)."""

ANALYZE_SCHEMA = {'type': 'object', 'additionalProperties': False,
                  'required': ['headline', 'summary', 'agreed_facts', 'discrepancies', 'discrepancy_score',
                               'score_reason', 'coverage_note', 'single_source_claims'],
                  'properties': {
                      'headline': {'type': 'string'}, 'summary': {'type': 'string'},
                      'discrepancy_score': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                      'score_reason': {'type': 'string'},
                      'agreed_facts': {'type': 'array', 'items': {'type': 'string'}},
                      'discrepancies': {'type': 'array', 'items': {
                          'type': 'object', 'additionalProperties': False,
                          'required': ['kind', 'significance', 'issue', 'versions', 'likely_explanation'],
                          'properties': {
                              'kind': {'type': 'string', 'enum': ['factual', 'framing', 'omission']},
                              'significance': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                              'issue': {'type': 'string', 'description': 'what the disagreement is about'},
                              'versions': {'type': 'array', 'items': {
                                  'type': 'object', 'additionalProperties': False,
                                  'required': ['source', 'says'],
                                  'properties': {'source': {'type': 'string'}, 'says': {'type': 'string'}}}},
                              'likely_explanation': {'type': 'string'}}}},
                      'coverage_note': {'type': 'string'},
                      'single_source_claims': {'type': 'array', 'items': {
                          'type': 'object', 'additionalProperties': False, 'required': ['source', 'claim'],
                          'properties': {'source': {'type': 'string'}, 'claim': {'type': 'string'}}}}}}

OVERVIEW_SYSTEM = """You write the top of a daily cross-outlet news brief. From the per-story analyses, write:
- brief: 3 sentences (about 80 words) covering the day's biggest news in plain language.
- watch: up to 3 of the day's most consequential discrepancies (one sentence each, under 25 words, naming the
  outlets). Use each story's discrepancy_score as a guide; skip differences that are just updated figures or
  detail level. If the outlets broadly agree today, list fewer, or none."""

OVERVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['brief', 'watch'],
                   'properties': {'brief': {'type': 'string'},
                                  'watch': {'type': 'array', 'items': {'type': 'string'}}}}


def cluster(items):
    lines = [f"{i['id']} | {i['source']} | {i['title']} | {i['summary'][:200]}" for i in items]
    out = claude(CLUSTER_SYSTEM, '\n'.join(lines), CLUSTER_SCHEMA, effort='medium')
    by_id = {i['id']: i for i in items}
    stories, used = [], set()
    for s in out['stories']:
        ids = [x for x in s['item_ids'] if x in by_id and x not in used]
        used.update(ids)
        if ids:
            stories.append({'title': s['title'], 'topic': s['topic'], 'items': [by_id[x] for x in ids]})
    return stories[:TOP_STORIES]


def analyze(story):
    per_source = {}
    for it in story['items']:
        per_source.setdefault(it['source'], []).append(it)
    blocks = []
    for source, its in per_source.items():
        for it in its[:PER_SOURCE_PER_STORY]:
            note = '' if len(it['text']) > 600 else ' [ONLY A SHORT FEED SUMMARY WAS AVAILABLE]'
            blocks.append(f"=== {source} | published {it['published'] or 'unknown'} | {it['title']}{note}\n"
                          f"{it['text']}")
    prompt = f"Story: {story['title']}\n\n" + '\n\n'.join(blocks)
    return claude(ANALYZE_SYSTEM, prompt, ANALYZE_SCHEMA, effort='medium')


def write_overview(stories):
    done = [s for s in stories if s.get('analysis')]
    overview_in = json.dumps([{'title': s['title'], 'sources': sorted({i['source'] for i in s['items']}),
                               **s['analysis']} for s in done], ensure_ascii=False)
    try:
        return claude(OVERVIEW_SYSTEM, overview_in, OVERVIEW_SCHEMA, effort='low')
    except Exception:
        return {'brief': '', 'watch': []}


def run(date):
    DATA.mkdir(parents=True, exist_ok=True)
    log('fetching feeds')
    items = fetch_items()
    log(f'{len(items)} items; clustering')
    stories = cluster(items)
    log(f'{len(stories)} stories: ' + '; '.join(s['title'] for s in stories))
    fetch_texts(stories)
    analyze_all(stories)

    log('writing overview')
    overview = write_overview(stories)
    save(date, len(items), overview, stories)


def fetch_texts(stories):
    to_fetch = []
    for s in stories:
        counts = {}
        for it in s['items']:
            counts[it['source']] = counts.get(it['source'], 0) + 1
            if counts[it['source']] <= PER_SOURCE_PER_STORY:
                to_fetch.append(it)
    log(f'fetching {len(to_fetch)} articles')
    # Playwright's sync API is not thread-safe, so browser pages load here, before the pool.
    pages = browser_get([it['link'] for it in to_fetch if it['source'] in BROWSER_SOURCES])
    for it in to_fetch:
        if it['link'] in pages:
            it['page'] = pages[it['link']]
    with ThreadPoolExecutor(8) as ex:
        for it, text in zip(to_fetch, ex.map(fetch_article, to_fetch)):
            it['text'] = text
    for s in stories:
        for it in s['items']:
            it.setdefault('text', it['summary'])


def analyze_all(stories):
    log('analyzing stories')

    def safe_analyze(s):
        try:
            return analyze(s)
        except Exception as e:
            log(f'analysis failed for {s["title"]}: {e}')
            return None
    with ThreadPoolExecutor(4) as ex:
        for s, a in zip(stories, ex.map(safe_analyze, stories)):
            s['analysis'] = a or s.get('analysis')   # a reanalyze failure keeps the old one
            log(f'done: {s["title"]} ({len(a["discrepancies"]) if a else "FAILED"})')


def save(date, item_count, overview, stories):
    for s in stories:
        for it in s['items']:
            it.pop('feed_content', None)
            it.pop('text', None)
    data = {'date': date, 'generated': datetime.now().astimezone().isoformat(timespec='minutes'),
            'sources': list(SOURCES), 'item_count': item_count, 'overview': overview, 'stories': stories}
    (DATA / f'{date}.json').write_text(json.dumps(data, ensure_ascii=False, indent=1))
    render(date)


# ---------------------------------------------------------------- render

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" '
         'href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family='
         'Playfair+Display:wght@700;900&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;'
         '1,8..60,400&family=Orbitron:wght@500;700&display=swap" rel="stylesheet">')

CSS = """
:root{--bg:#f4f1ea;--paper:#fbf9f4;--ink:#18181a;--muted:#5d5b56;--rule:#18181a;--hair:#d6d1c4;
--hi:#b3261e;--md:#b26b00;--lo:#7b7f86;--ok:#2e7d32;--chip:#ebe6da;--box:#f0ebdf;--brand:#00782a;--glow:none}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121212;--paper:#191918;--ink:#ebe8e1;
--muted:#a4a19a;--rule:#ebe8e1;--hair:#34332f;--hi:#f2766b;--md:#e6a542;--lo:#8f939a;--ok:#5cc970;--chip:#2a2926;
--box:#211f1c;--brand:#00ff41;--glow:0 0 8px rgba(0,255,65,.45)}}
:root[data-theme=dark]{--bg:#121212;--paper:#191918;--ink:#ebe8e1;--muted:#a4a19a;--rule:#ebe8e1;--hair:#34332f;
--hi:#f2766b;--md:#e6a542;--lo:#8f939a;--ok:#5cc970;--chip:#2a2926;--box:#211f1c;--brand:#00ff41;
--glow:0 0 8px rgba(0,255,65,.45)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 "Source Serif 4",Georgia,serif}
.sheet{max-width:1180px;margin:0 auto;padding:22px 16px 60px}
.sans{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
.mast{text-align:center;border-top:1px solid var(--rule);padding-top:10px}
.mast h1{font:900 clamp(40px,7vw,76px)/1 "Playfair Display",Georgia,serif;margin:6px 0 8px;letter-spacing:-.01em}
.brand{display:inline-flex;align-items:center;gap:9px;margin-top:6px;color:var(--brand);text-decoration:none;
font:700 13px/1 Orbitron,sans-serif;letter-spacing:.24em;text-shadow:var(--glow)}
.brand i{width:15px;height:15px;background:var(--brand);clip-path:polygon(50% 0,100% 25%,100% 75%,50% 100%,0 75%,0 25%)}
.tagline{margin:0 0 12px;font:500 11px/1.4 Orbitron,sans-serif;letter-spacing:.2em;text-transform:uppercase;color:var(--muted)}
.tagline span{white-space:nowrap}
.stats{font-family:Orbitron,sans-serif;font-weight:500;color:var(--brand);text-shadow:var(--glow);letter-spacing:.12em}
.dateline{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;border-top:3px double var(--rule);
border-bottom:3px double var(--rule);padding:6px 2px;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.days{display:flex;justify-content:center;gap:6px;flex-wrap:wrap;margin:8px 0 0;font-size:12px}
.days a{color:var(--muted);text-decoration:none;padding:1px 8px;border-radius:99px}
.days a.cur{color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink)}
.inbrief{display:grid;grid-template-columns:1.25fr 1fr;gap:28px;padding:18px 0;border-bottom:1px solid var(--rule)}
.inbrief p{margin:0;font-size:19px;line-height:1.5;font-style:italic}
.label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:var(--hi);margin:0 0 6px}
.inbrief ul{margin:0;padding:0;list-style:none;font-size:14px;line-height:1.45}
.inbrief li{padding:5px 0 5px 14px;border-top:1px solid var(--hair);position:relative}
.inbrief li:first-child{border-top:0;padding-top:0}
.inbrief li::before{content:"";position:absolute;left:0;top:.62em;width:6px;height:6px;background:var(--hi);border-radius:50%}
.inbrief li:first-child::before{top:.4em}
.lead{display:grid;grid-template-columns:2fr 1fr;gap:28px;padding:22px 0;border-bottom:1px solid var(--rule)}
.kicker{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}
.lead h2{font:900 clamp(30px,4.2vw,46px)/1.08 "Playfair Display",Georgia,serif;margin:6px 0 12px}
.lead .body{column-count:2;column-gap:26px;column-rule:1px solid var(--hair);font-size:17px}
.lead .body p{margin:0 0 10px}
.row{display:grid;grid-template-columns:repeat(3,1fr);padding:20px 0;border-bottom:1px solid var(--rule)}
.row>section{padding:0 22px;border-left:1px solid var(--hair)}
.row>section:first-child{padding-left:0;border-left:0}.row>section:last-child{padding-right:0}
.row h2{font:700 24px/1.15 "Playfair Display",Georgia,serif;margin:6px 0 10px}
.row p{margin:0 0 10px;font-size:16px}
.note{color:var(--muted);font-style:italic;font-size:15px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin:0 0 10px}
.chip{font-size:11px;padding:1px 8px;border-radius:99px;background:var(--chip);border-left:3px solid var(--c)}
.chip.absent{opacity:.4;border-left-color:var(--hair)}
.differ{background:var(--box);border-top:3px solid var(--hi);padding:12px 14px;font-size:14px;line-height:1.45}
.differ .label{margin-bottom:8px}
.flag{padding:8px 0;border-top:1px solid var(--hair)}.flag:first-of-type{border-top:0;padding-top:0}
.flag .issue{font-weight:600;display:block;margin:2px 0 4px}
.flag ul{margin:0;padding:0;list-style:none}.flag li{margin:3px 0}
.flag li b{color:var(--c)}
.sev{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--sev)}
.high{--sev:var(--hi)}.medium{--sev:var(--md)}.low{--sev:var(--lo)}
.calm{color:var(--muted);font-size:14px;margin:0}
.meter{display:flex;align-items:center;gap:8px;font-size:12px;margin:0 0 10px;color:var(--muted)}
.meter .bar{flex:0 0 90px;height:6px;border-radius:3px;background:var(--hair);overflow:hidden}
.meter .bar i{display:block;height:100%;background:var(--m)}
.meter b{color:var(--m);font-size:13px}
.meter .why{flex-basis:100%;margin-top:-4px}.meter{flex-wrap:wrap}
.m-ok{--m:var(--ok)}.m-lo{--m:var(--lo)}.m-md{--m:var(--md)}.m-hi{--m:var(--hi)}
details.full{margin-top:12px;font-size:14px}
details.full>summary{cursor:pointer;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
h4{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:16px 0 6px}
.disc{border-left:3px solid var(--sev);padding:8px 12px;margin:8px 0;background:var(--box)}
.disc .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.disc .issue{font-weight:600;flex-basis:100%;margin-top:2px}
.badge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--sev)}
.kind{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.versions{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:6px;margin:8px 0 4px}
.ver{background:var(--paper);border-top:3px solid var(--c);padding:6px 9px;font-size:13.5px}
.ver b{display:block;font-size:11px}
.why{font-size:12.5px;color:var(--muted)}
ul.facts{margin:0;padding-left:18px}ul.facts li{margin:3px 0}
.links{margin:6px 0 0;padding-left:18px}.links a{color:var(--ink)}
details.minor{margin:6px 0}details.minor>summary{cursor:pointer;color:var(--muted);font-size:13px}
.inside{padding:20px 0;border-bottom:1px solid var(--rule)}
.inside h3{font:700 22px/1 "Playfair Display",Georgia,serif;margin:0 0 12px}
.inside .list{display:grid;grid-template-columns:1fr 1fr;column-gap:28px}
details.story{border-top:1px solid var(--hair);padding:10px 0}
details.story>summary{cursor:pointer;list-style:none;display:flex;gap:10px;align-items:baseline}
details.story>summary::-webkit-details-marker{display:none}
details.story>summary .t{font-weight:600;font-size:17px;line-height:1.3;flex:1}
details.story>summary .n{font-size:11px;font-weight:700;color:var(--hi);white-space:nowrap}
details.story[open]{padding-bottom:16px}
details.story .inner{padding-top:8px}
footer{color:var(--muted);font-size:12.5px;margin-top:18px;line-height:1.5}
@media (max-width:860px){.inbrief,.lead,.row,.inside .list{grid-template-columns:1fr}
.lead .body{column-count:1}.row>section{padding:16px 0 0;border-left:0;border-top:1px solid var(--hair);margin-top:16px}
.row>section:first-child{padding-top:0;border-top:0;margin-top:0}.dateline{justify-content:center}}
"""

SEV_ORDER = {'high': 0, 'medium': 1, 'low': 2}
FRONT_PAGE = 4


def esc(s):
    return html.escape(str(s or ''))


def color_for(source):
    for k, c in SOURCE_COLORS.items():
        if k.lower() in source.lower() or source.lower() in k.lower():
            return c
    return '#888'


def disc_html(d):
    vers = ''.join(f'<div class="ver" style="--c:{color_for(v["source"])}"><b>{esc(v["source"])}</b>'
                   f'{esc(v["says"])}</div>' for v in d['versions'])
    return (f'<div class="disc {esc(d["significance"])}"><div class="top">'
            f'<span class="badge">{esc(d["significance"])}</span><span class="kind">{esc(d["kind"])}</span>'
            f'<span class="issue">{esc(d["issue"])}</span></div><div class="versions">{vers}</div>'
            f'<div class="why">Likely explanation: {esc(d["likely_explanation"])}</div></div>')


def flags(a):
    discs = sorted(a['discrepancies'], key=lambda d: SEV_ORDER.get(d['significance'], 3))
    return [d for d in discs if d['significance'] != 'low'], [d for d in discs if d['significance'] == 'low']


SCORE_BANDS = [(10, 'ok', 'Sources align'), (30, 'lo', 'Minor differences'), (60, 'md', 'Notable differences'),
               (85, 'hi', 'Conflicting accounts'), (100, 'hi', 'Contradictory')]


def band(score):
    return next((cls, label) for top, cls, label in SCORE_BANDS if score <= top)


def meter_html(score, why='', prefix='Discrepancy'):
    """1-100 bar; analyses saved before the score existed have none and get no meter."""
    if score is None:
        return ''
    cls, label = band(score)
    why = f'<span class="why">{esc(why)}</span>' if why else ''
    return (f'<div class="meter sans m-{cls}" title="1 = outlets tell the same story, 100 = contradictory accounts">'
            f'<span class="bar"><i style="width:{score}%"></i></span><span>{esc(prefix)} <b>{score}</b>/100 · '
            f'{label}</span>{why}</div>')


def day_score(stories):
    scores = [s['analysis']['discrepancy_score'] for s in stories if 'discrepancy_score' in s['analysis']]
    return round(sum(scores) / len(scores)) if scores else None


def chips_html(s, sources):
    present = list(dict.fromkeys(it['source'] for it in s['items']))
    return ('<div class="chips sans">' +
            ''.join(f'<span class="chip" style="--c:{color_for(x)}">{esc(x)}</span>' for x in present) +
            ''.join(f'<span class="chip absent" title="not in this outlet\'s feeds today">{esc(x)}</span>'
                    for x in sources if x not in present) + '</div>')


def differ_html(a, limit):
    """The 'where they differ' box: the top non-minor flags, compact."""
    main, _ = flags(a)
    meter = meter_html(a.get('discrepancy_score'), a.get('score_reason', ''))
    if not main:
        return ('<div class="differ sans"><div class="label">Where they differ</div>' + meter +
                '<p class="calm">Outlets broadly agree on this one.</p></div>')
    out = ['<div class="differ sans"><div class="label">Where they differ</div>' + meter]
    for d in main[:limit]:
        vers = ''.join(f'<li style="--c:{color_for(v["source"])}"><b>{esc(v["source"])}:</b> {esc(v["says"])}</li>'
                       for v in d['versions'])
        out.append(f'<div class="flag {esc(d["significance"])}"><span class="sev">{esc(d["significance"])} · '
                   f'{esc(d["kind"])}</span><span class="issue">{esc(d["issue"])}</span><ul>{vers}</ul></div>')
    return ''.join(out) + '</div>'


def full_html(s, open_label):
    a = s['analysis']
    main, minor = flags(a)
    out = [f'<details class="full sans"><summary>{esc(open_label)}</summary>',
           f'<h4>Discrepancies ({len(main)})</h4>']
    if not main:
        out.append('<p class="calm">No meaningful disagreements between outlets.</p>')
    out.extend(disc_html(d) for d in main)
    if minor:
        out.append(f'<details class="minor"><summary>{len(minor)} minor difference{"s" if len(minor) != 1 else ""} '
                   f'(wording, detail level)</summary>' + ''.join(disc_html(d) for d in minor) + '</details>')
    if a.get('agreed_facts'):
        out.append('<h4>Agreed facts</h4><ul class="facts">' +
                   ''.join(f'<li>{esc(f)}</li>' for f in a['agreed_facts']) + '</ul>')
    if a.get('single_source_claims'):
        out.append('<h4>Only one outlet reports</h4><ul class="facts">' +
                   ''.join(f'<li><b>{esc(c["source"])}:</b> {esc(c["claim"])}</li>'
                           for c in a['single_source_claims']) + '</ul>')
    out.append('<h4>Source articles</h4><ul class="links">' + ''.join(
        f'<li>{esc(it["source"])}: <a href="{esc(it["link"])}" target="_blank" rel="noopener">'
        f'{esc(it["title"])}</a></li>' for it in s['items']) + '</ul></details>')
    return ''.join(out)


def flag_count(a):
    main, _ = flags(a)
    hi = sum(d['significance'] == 'high' for d in main)
    score = f'{a["discrepancy_score"]}/100 · ' if 'discrepancy_score' in a else ''
    return score + f'{len(main)} flag{"s" if len(main) != 1 else ""}' + (f' · {hi} major' if hi else '')


def render(date):
    data = json.loads((DATA / f'{date}.json').read_text())
    days = sorted((p.stem for p in DATA.glob('*.json')), reverse=True)
    pretty = datetime.strptime(date, '%Y-%m-%d').strftime('%A, %B %-d, %Y')
    stories = [s for s in data['stories'] if s.get('analysis')]
    srcs = data['sources']

    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>Daily Brief {esc(date)}</title>{FONTS}<style>{CSS}</style></head><body><div class="sheet">']
    out.append(f'<header class="mast"><a class="brand" href="https://integrated-dynamics.net/"><i></i>INTEGRATED DYNAMICS</a>'
               f'<h1>The Daily Brief</h1><p class="tagline"><span>Aggregate sources |</span> <span>Identify discrepancies</span></p>'
               f'<div class="dateline sans"><span>{esc(pretty)}</span><span>{esc(" · ".join(srcs))}</span>'
               f'<span class="stats">{data["item_count"]} reports · {len(stories)} stories · {esc(data["generated"][11:16])}</span></div><nav class="days sans">' + ''.join(
                   f'<a href="{d}.html"{" class=cur" if d == date else ""}>{d[5:]}</a>' for d in days[:14]) +
               '</nav></header>')

    ov = data.get('overview') or {}
    if ov.get('brief'):
        today = day_score(stories)
        watch = meter_html(today, prefix="Today's discrepancy level")
        if ov.get('watch'):
            watch += ('<div class="label">Biggest discrepancies</div><ul>' +
                      ''.join(f'<li>{esc(w)}</li>' for w in ov['watch']) + '</ul>')
        elif today is not None:
            watch += '<p class="calm">The outlets broadly agree on today\'s top stories.</p>'
        watch = f'<div class="sans">{watch}</div>' if watch else ''
        out.append(f'<section class="inbrief"><div><div class="label sans">In brief</div>'
                   f'<p>{esc(ov["brief"])}</p></div>{watch}</section>')

    if stories:
        s = stories[0]
        a = s['analysis']
        note = f'<p class="note">{esc(a["coverage_note"])}</p>' if a.get('coverage_note') else ''
        out.append(f'<article class="lead"><div><div class="kicker sans">{esc(s["topic"])}</div>'
                   f'<h2>{esc(a["headline"])}</h2>{chips_html(s, srcs)}<div class="body"><p>{esc(a["summary"])}</p>'
                   f'{note}</div>{full_html(s, "Full comparison · " + flag_count(a))}</div>'
                   f'{differ_html(a, 2)}</article>')

    row = stories[1:FRONT_PAGE]
    if row:
        out.append('<div class="row">')
        for s in row:
            a = s['analysis']
            out.append(f'<section><div class="kicker sans">{esc(s["topic"])}</div><h2>{esc(a["headline"])}</h2>'
                       f'{chips_html(s, srcs)}<p>{esc(a["summary"])}</p>{differ_html(a, 1)}'
                       f'{full_html(s, "Full comparison · " + flag_count(a))}</section>')
        out.append('</div>')

    rest = stories[FRONT_PAGE:]
    if rest:
        out.append('<section class="inside"><h3>Also today</h3><div class="list">')
        for s in rest:
            a = s['analysis']
            out.append(f'<details class="story"><summary><span class="t">{esc(a["headline"])}</span>'
                       f'<span class="n sans">{esc(flag_count(a))}</span></summary><div class="inner">'
                       f'{chips_html(s, srcs)}<p>{esc(a["summary"])}</p>{differ_html(a, 1)}'
                       f'{full_html(s, "Full comparison")}</div></details>')
        out.append('</div></section>')

    failed = [s['title'] for s in data['stories'] if not s.get('analysis')]
    if failed:
        out.append('<p class="calm sans">Analysis failed for: ' + esc('; '.join(failed)) + '</p>')
    out.append('<footer class="sans">Discrepancies are flagged by an AI comparison of each outlet\'s article text; '
               'check the source links before relying on any one flag. Grayed-out outlets just weren\'t in that '
               'outlet\'s RSS feeds in the window, which doesn\'t prove they skipped the story.</footer>'
               '</div></body></html>')

    page = ''.join(out)
    (SITE / f'{date}.html').write_text(page)
    if date == days[0]:
        (SITE / 'index.html').write_text(page)
    log(f'wrote {SITE / (date + ".html")}')


# ---------------------------------------------------------------- publish

PUBLIC_URL = 'https://integrated-dynamics.net/news_brief/'
WEBSITE_CLONE = Path('/home/web/Programs/website_clone')
PUBLISH_DIR = WEBSITE_CLONE / 'clients/IntegratedDynamics/news_brief'   # gitignored there; this repo is the source


def publish():
    """Copy the pages into the integrated-dynamics.net site and deploy it to Netlify.

    The copies get <base href="/news_brief/"> so their relative day links work whether the visitor
    came in on /news_brief or /news_brief/, and noindex so the paraphrased reporting stays out of
    search under the business domain. Days that left site/ are removed from the public copy too.
    """
    PUBLISH_DIR.mkdir(exist_ok=True)
    pages = {p.name: p for p in SITE.glob('*.html')}
    for old in PUBLISH_DIR.glob('*.html'):
        if old.name not in pages:
            old.unlink()
    head = '<head><base href="/news_brief/"><meta name="robots" content="noindex">'
    for name, page in pages.items():
        (PUBLISH_DIR / name).write_text(page.read_text().replace('<head>', head, 1))
    write_latest()
    deploy = subprocess.run(
        [str(WEBSITE_CLONE.parent / 'venv/bin/python'), '-c',
         "import ads_connector as a; print(a.deploy_to_netlify('integrated-dynamics.net'))"],
        cwd=WEBSITE_CLONE, capture_output=True, text=True, timeout=600)
    if deploy.returncode:
        raise RuntimeError(f'deploy failed:\n{deploy.stderr.strip() or deploy.stdout.strip()}')
    log(f'published {len(pages)} pages to {PUBLIC_URL}')


def write_latest():
    """Write news_brief/latest.json for the integrated-dynamics.net home page teaser.

    Only counts, story titles and the discrepancy flags go out, not the paraphrased brief.
    """
    path = max(DATA.glob('*.json'), default=None)
    if not path:
        return
    data = json.loads(path.read_text())
    score = day_score(data['stories'])
    latest = {
        'date': data['date'],
        'generated': data['generated'],
        'level': score,                                     # 1-100, None before scores existed
        'level_band': band(score) if score is not None else None,   # [css class, label]
        'sources': data['sources'],
        'article_count': data['item_count'],
        'story_count': len(data['stories']),
        'headlines': [s['title'] for s in data['stories'][:4]],
        'flags': (data.get('overview') or {}).get('watch', []),
    }
    (PUBLISH_DIR / 'latest.json').write_text(json.dumps(latest, ensure_ascii=False, indent=1))



if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['run', 'render', 'overview', 'reanalyze', 'publish'])
    ap.add_argument('--date', default=datetime.now().strftime('%Y-%m-%d'))
    args = ap.parse_args()
    if args.cmd == 'run':
        run(args.date)
    elif args.cmd == 'overview':   # rewrite just the top-of-page brief from saved analyses
        path = DATA / f'{args.date}.json'
        data = json.loads(path.read_text())
        data['overview'] = write_overview(data['stories'])
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        render(args.date)
    elif args.cmd == 'reanalyze':   # same stories, fresh article text, current prompts
        data = json.loads((DATA / f'{args.date}.json').read_text())
        fetch_texts(data['stories'])
        analyze_all(data['stories'])
        save(args.date, data['item_count'], write_overview(data['stories']), data['stories'])
    elif args.cmd == 'publish':
        publish()
    else:
        render(args.date)
