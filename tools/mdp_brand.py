"""MDataPulse Reports: logo, top bar, footer and home page for tools/rca_server.py.

Only the server's own pages get the full look. Reports (RCA and health) get a slim bar on
screen only: it is hidden when printing, and the report HTML/CSS itself is not changed.
"""
import base64
import html
import os
import re
import time

NAME = "MDataPulse Reports"
# Settings: MDP_OWNER (who runs the service), MDP_CLIENT (optional, "for the ..."), MDP_OWNER_LOGO (image file
# shown in the footer, optional), MDP_SEARCH_HINT (example text in the device search box).
OWNER = os.environ.get("MDP_OWNER", "MDataPulse")
CLIENT = os.environ.get("MDP_CLIENT", "")
SEARCH_HINT = os.environ.get("MDP_SEARCH_HINT", "Device name or IP address")
TAG_IMG, TAG_LINK, TAG_STYLE, DATA = "img", "link", "style", "data" + ":"



def _owner_logo():
    path = os.environ.get("MDP_OWNER_LOGO", "")
    try:
        raw = open(path, "rb").read() if path else b""
    except OSError:
        raw = b""
    if not raw:
        return ""
    kind = "png" if raw[:4] == b"\x89PNG" else "svg+xml" if b"<svg" in raw[:400] else "jpeg"
    return DATA + f"image/{kind};base64," + base64.b64encode(raw).decode()


HT_LOGO = _owner_logo()

# The MDataPulse mark: a pulse line drawn as an "M" on a navy tile, with a green data dot.
MARK = ("<svg xmlns='http://www.w3.org/2000/svg' class='mdp-mark' viewBox='0 0 48 48' width='{s}' height='{s}' aria-hidden='true'>"
        "<rect width='48' height='48' rx='11' fill='#13245e'/>"
        "<path d='M5 30h6l4-17 9 19 9-19 4 17h6' fill='none' stroke='#ff4d4f' stroke-width='4.2' "
        "stroke-linecap='round' stroke-linejoin='round'/>"
        "<circle cx='43' cy='30' r='3.4' fill='#8dc63f'/></svg>")

FAVICON = (DATA + "image/svg+xml," + MARK.format(s=48).replace("class='mdp-mark' ", "")
           .replace("#", "%23").replace("<", "%3C").replace(">", "%3E").replace("'", "%27"))


def wordmark(tag="Reports"):
    return (f"<span class='mdp-word'><span class='mdp-m'>M</span>DataPulse"
            f"{' <span class=mdp-tag>' + html.escape(tag) + '</span>' if tag else ''}</span>")


CSS = """
.mdp-bar,.mdp-foot{font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;box-sizing:border-box}
.mdp-bar *,.mdp-foot *{box-sizing:border-box}
.mdp-bar{background:linear-gradient(90deg,#0b1640,#16285f 60%,#1b3270);color:#fff;
 margin:-24px -16px 24px;padding:10px 20px;display:flex;align-items:center;gap:18px;flex-wrap:wrap;
 border-bottom:3px solid #8dc63f}
.mdp-brand{display:flex;align-items:center;gap:10px;color:#fff!important;text-decoration:none!important}
.mdp-word{font-weight:800;font-size:19px;letter-spacing:-.01em;color:#fff}
.mdp-m{color:#ff4d4f}.mdp-tag{font-weight:500;color:#b9c6e8;font-size:15px;margin-left:2px}
.mdp-nav{display:flex;gap:4px;flex-wrap:wrap;margin-left:auto}
.mdp-nav a{color:#dbe4ff!important;text-decoration:none!important;padding:6px 12px;border-radius:7px;font-size:13.5px;font-weight:600}
.mdp-nav a:hover{background:rgba(255,255,255,.10);color:#fff!important}
.mdp-nav a.on{background:rgba(141,198,63,.18);color:#fff!important;box-shadow:inset 0 -2px 0 #8dc63f}
.mdp-foot{margin:40px -16px -24px;padding:16px 20px;border-top:1px solid rgba(127,127,127,.25);
 display:flex;align-items:center;gap:14px;flex-wrap:wrap;color:#667085;font-size:12.5px}
.mdp-foot img{height:56px;width:auto;background:#fff;border-radius:6px;padding:3px 5px}
.mdp-foot b{color:inherit}.mdp-foot .mdp-r{margin-left:auto}
@media (prefers-color-scheme:dark){.mdp-foot{color:#98a2b3}}
@media print{.mdp-bar,.mdp-foot,.toolbar{display:none!important}}
/* MDP-SCALE: our own pages (not the reports) fill the window, footer at the bottom, larger on big screens */
body.mdp-app{--z:1;zoom:var(--z);min-height:calc(100vh / var(--z));display:flex;flex-direction:column;box-sizing:border-box}
body.mdp-app>main{width:100%;flex:1 0 auto}body.mdp-app>.mdp-foot{margin-top:40px}
@media (min-width:1500px){body.mdp-app{--z:1.2}}
@media (min-width:1800px){body.mdp-app{--z:1.4}}
@media (min-width:2300px){body.mdp-app{--z:1.7}}
@media (min-width:3000px){body.mdp-app{--z:2.2}}
@media print{body.mdp-app{zoom:1;min-height:0;display:block}}
"""

HOME_CSS = """
.mdp-hero{display:flex;align-items:center;gap:22px;flex-wrap:wrap;padding:26px 28px;border-radius:16px;margin:4px 0 22px;
 background:linear-gradient(135deg,#0b1640 0%,#1b3270 70%,#24407f 100%);color:#fff;position:relative;overflow:hidden}
.mdp-hero:after{content:"";position:absolute;right:-60px;top:-60px;width:260px;height:260px;border-radius:50%;
 border:28px solid rgba(141,198,63,.14)}
.mdp-hero h1{font-size:32px;margin:0;color:#fff;letter-spacing:-.02em}.mdp-hero h1 .mdp-tag{font-size:22px}
.mdp-hero p{margin:6px 0 0;color:#c7d2f0;max-width:620px}
.mdp-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.mdp-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;display:flex;flex-direction:column;gap:10px;
 border-top:4px solid var(--c,#2563eb)}
.mdp-card h2{margin:0;font-size:18px;display:flex;align-items:center;gap:10px}
.mdp-card h2 svg{flex:none}.mdp-card p{margin:0;color:var(--muted)}
.mdp-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:auto;padding-top:6px}
.mdp-links a{font-size:13px;padding:6px 11px;border-radius:7px;border:1px solid var(--line);text-decoration:none;color:var(--fg);background:var(--bg)}
.mdp-links a.pri{background:var(--c,#2563eb);border-color:var(--c,#2563eb);color:#fff;font-weight:600}
.mdp-links a:hover{border-color:var(--c,#2563eb)}
.mdp-search{display:flex;gap:8px}.mdp-search input{flex:1;min-width:0}
"""

NAV = [("home", "./", "Home"), ("reports", "reports", "Health reports"), ("logs", "logs", "Device logs"),
       ("signal", "signal", "Radio signal"), ("endpoint_signal", "endpoint-signal", "Endpoint signal"),  # MDP-ENDPOINT_SIGNAL
       ("netmap", "netmap", "Network map"), ("export", "export", "Live export"),  # MDP-EXPORT
       ("rca", "rca", "RCA report")]  # MDP-SIGNAL MDP-NETMAP


def bar(active="", base="./"):
    """Top bar. base = relative path from the current page back to the server root."""
    links = "".join(f"<a href='{base}{'' if h == './' else h}'{' class=on' if k == active else ''}>{t}</a>"
                    for k, h, t in NAV)
    return (f"<div class='mdp-bar'><a class='mdp-brand' href='{base}'>{MARK.format(s=34)}{wordmark()}</a>"
            f"<nav class='mdp-nav'>{links}</nav></div>")


def footer():
    logo = f"<{TAG_IMG} src='{HT_LOGO}' alt='{html.escape(OWNER, True)}'>" if HT_LOGO else ""
    made = f"Created by <b>{html.escape(OWNER)}</b>" + (f" for the {html.escape(CLIENT)}" if CLIENT else "")
    return (f"<div class='mdp-foot'>{logo}"
            f"<div><b>{NAME}</b><br>{made}.</div>"
            f"<div class='mdp-r'>&copy; {time.localtime().tm_year} {html.escape(OWNER)}. All rights reserved.</div></div>")


def head_bits():
    return f"<{TAG_LINK} rel='icon' href=\"{FAVICON}\"><{TAG_STYLE}>{CSS}</{TAG_STYLE}>"


def decorate(doc, active="", base="./", retitle=True):
    """Add the favicon, top bar and footer to a whole HTML page. Report pages keep their own CSS;
    the bar and footer are hidden in print, so printed reports look exactly as before."""
    if "mdp-bar" in doc:
        return doc
    if retitle:
        doc = re.sub(r"<title>(.*?)</title>", lambda m: f"<title>{m.group(1)} · {NAME}</title>", doc, count=1)
    doc = doc.replace("</head>", head_bits() + "</head>", 1)
    cls = " class='mdp-app'" if retitle else ""  # retitle=False: a report, keep its layout as is
    doc = re.sub(r"<body([^>]*)>", lambda m: f"<body{m.group(1)}{cls}>" + bar(active, base), doc, count=1)
    i = doc.rfind("</body>")
    return doc[:i] + footer() + doc[i:] if i >= 0 else doc + footer()


ICONS = {
    "reports": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
               "stroke-linecap='round' stroke-linejoin='round'><path d='M3 3v18h18'/><path d='M7 15l4-4 3 3 5-6'/></svg>",
    "logs": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
            "stroke-linecap='round' stroke-linejoin='round'><path d='M8 6h13M8 12h13M8 18h13'/><path d='M3 6h.01M3 12h.01M3 18h.01'/></svg>",
    "rca": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
           "stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><path d='M21 21l-4.3-4.3'/>"
           "<path d='M8 11h2l1-2 2 4 1-2h1'/></svg>",
    "signal": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
              "stroke-linecap='round' stroke-linejoin='round'><path d='M4 20v-3M9 20v-7M14 20v-11M19 20V4'/></svg>",  # MDP-SIGNAL
    "endpoint_signal": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
               "stroke-linecap='round' stroke-linejoin='round'><path d='M12 9v12M8 21h8'/><circle cx='12' cy='7' r='2'/>"
               "<path d='M8.5 3.5a5 5 0 0 0 0 7M15.5 3.5a5 5 0 0 1 0 7'/></svg>",  # MDP-ENDPOINT_SIGNAL
    "netmap": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
              "stroke-linecap='round' stroke-linejoin='round'><path d='M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z'/>"
              "<path d='M9 4v14M15 6v14'/></svg>",  # MDP-NETMAP
    "export": "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' "
              "stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='3' width='18' height='18' rx='2'/>"
              "<path d='M3 9h18M9 21V9'/><path d='M14 14l3 3 3-3M17 11v6' transform='translate(-2 -1)'/></svg>",  # MDP-EXPORT
}


def home(zabbix_url=""):
    zbx = (f"<a href='{html.escape(zabbix_url)}/zabbix.php?action=dashboard.view'>Open Zabbix</a>"
           if zabbix_url else "")
    return f"""
<{TAG_STYLE}>{HOME_CSS}</{TAG_STYLE}>
<section class='mdp-hero'>{MARK.format(s=76)}
 <div><h1>{wordmark()}</h1>
 <p>Network health reports, device logs and root cause analysis for the network, built from Zabbix
 monitoring data.</p></div></section>
<div class='mdp-cards'>
 <div class='mdp-card' style='--c:#2563eb'><h2 style='color:#2563eb'>{ICONS['reports']}<span style='color:var(--fg)'>Health reports</span></h2>
  <p>Daily, weekly and monthly network health, plus the Zabbix admin report. Built every morning,
  printable and ready to save as PDF.</p>
  <div class='mdp-links'><a class='pri' href='reports'>All reports</a><a href='reports/now/daily'>Today so far</a>
  <a href='reports/now/weekly'>This week</a><a href='reports/now/monthly'>This month</a>
  <a href='reports/now/admin'>Zabbix admin</a></div></div>
 <div class='mdp-card' style='--c:#16a34a'><h2 style='color:#16a34a'>{ICONS['logs']}<span style='color:var(--fg)'>Device logs</span></h2>
  <p>Every alarm, outage and change for one device over the last days. Search by name or IP address.</p>
  <form class='mdp-search' action='logs' method='get'><input name='q' placeholder='{html.escape(SEARCH_HINT, True)}'>
  <button type='submit'>Search</button></form>
  <div class='mdp-links'><a class='pri' href='logs'>Open device logs</a></div></div>
 <div class='mdp-card' style='--c:#e5383b'><h2 style='color:#e5383b'>{ICONS['rca']}<span style='color:var(--fg)'>RCA report</span></h2>
  <p>Pick a site and the time of an incident. The report reads ping, uptime, radio, PoE and UPS history and says
  whether it was power, connectivity, upstream or radio.</p>
  <div class='mdp-links'><a class='pri' href='rca'>New RCA report</a>{zbx}</div></div>
 <div class='mdp-card' style='--c:#7c3aed'><h2 style='color:#7c3aed'>{ICONS['signal']}<span style='color:var(--fg)'>Radio signal check</span></h2>
  <p>Signal and SNR of every site radio at the busy hour, compared with the day before and the week before.</p>
  <div class='mdp-links'><a class='pri' href='signal/now/daily'>Latest daily</a><a href='signal/now/weekly'>This week</a><a href='signal'>All checks</a></div></div>
 <div class='mdp-card' style='--c:#0d9488'><h2 style='color:#0d9488'>{ICONS['endpoint_signal']}<span style='color:var(--fg)'>Endpoint signal check</span></h2>
  <p>Signal and SNR of every endpoint CPE at the busy hour, grouped by site, with the sector AP it is connected to.</p>
  <div class='mdp-links'><a class='pri' href='endpoint-signal/now/daily'>Latest daily</a><a href='endpoint-signal/now/weekly'>This week</a><a href='endpoint-signal'>All checks</a></div></div>
 <div class='mdp-card' style='--c:#0891b2'><h2 style='color:#0891b2'>{ICONS['netmap']}<span style='color:var(--fg)'>Network map</span></h2>
  <p>Every site and endpoint on the map, live: links between sites turn red when down, a endpoint turns red when it is disconnected.</p>
  <div class='mdp-links'><a class='pri' href='netmap'>Open the map</a></div></div>
 <div class='mdp-card' style='--c:#ea580c'><h2 style='color:#ea580c'>{ICONS['export']}<span style='color:var(--fg)'>Zabbix Live Export</span></h2>
  <p>Any Zabbix dashboard window exactly as it shows now: same devices, columns, order and colours. Save it as Excel, CSV or PDF.</p>
  <div class='mdp-links'><a class='pri' href='export'>Pick a window</a></div></div>
</div>"""
