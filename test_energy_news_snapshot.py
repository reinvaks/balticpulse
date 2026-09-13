import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

xml = """<?xml version="1.0"?>
<rss><channel>
<item>
<title>Europe gas prices rise on storage concerns - Reuters</title>
<link>https://news.google.com/rss/articles/example</link>
<pubDate>Sun, 13 Sep 2026 10:00:00 GMT</pubDate>
<description>European gas markets react to storage levels.</description>
</item>
</channel></rss>"""
root = ET.fromstring(xml)
items = root.findall(".//item")
assert len(items) == 1
assert items[0].findtext("title").endswith(" - Reuters")
assert parsedate_to_datetime(items[0].findtext("pubDate")).year == 2026
print("energy news RSS parser fixture: OK")
