"""Validate local website links, assets and fragment targets without a browser."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
root = Path(__file__).resolve().parents[1] / 'site'
class Page(HTMLParser):
    def __init__(self, file):
        super().__init__(); self.links = []; self.ids = set(); self.duplicates = []; self.file = file
        self.feed(file.read_text())
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            if attrs['id'] in self.ids: self.duplicates.append(attrs['id'])
            self.ids.add(attrs['id'])
        for name in ('href', 'src', 'poster'):
            if attrs.get(name): self.links.append(attrs[name])
pages = {p.resolve(): Page(p) for p in root.glob('*.html')}
errors = []
for file, page in pages.items():
    errors.extend(f'{file.name}: duplicate id {i}' for i in page.duplicates)
    for raw in page.links:
        u = urlsplit(raw)
        if u.scheme or u.netloc: continue
        target = (file.parent / unquote(u.path)).resolve() if u.path else file
        if target.is_dir(): target = target / 'index.html'
        if not target.exists(): errors.append(f'{file.name}: missing {raw}'); continue
        if u.fragment and target in pages and unquote(u.fragment) not in pages[target].ids:
            errors.append(f'{file.name}: missing fragment {raw}')
if errors: raise SystemExit('\n'.join(errors))
print(f'Website: {len(pages)} HTML pages; local links, assets and fragments verified.')
