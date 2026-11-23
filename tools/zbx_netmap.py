#!/usr/bin/env python3
"""Network geo map (MDP-NETMAP): every site and endpoint on a real map, live from Zabbix.

  /netmap            the map page (MDataPulse Reports); ?embed=1 = without the top bar, for the Zabbix dashboard
  /netmap/data       the live state as JSON (the page reads it every 30 seconds)
  /netmap/leaflet.js the map library, read from the Zabbix frontend on this server

  python3 tools/zbx_netmap.py check                       what matches between the Excel locations and Zabbix
  python3 tools/zbx_netmap.py dashboard [--apply]         put the map on the "Sites Overview" dashboard in place
          [--name "Sites Overview"] [--url http://SERVER:8081]   of the old branch map (and let the frame run it)
  python3 tools/zbx_netmap.py inventory [--apply]         optional: write the locations into the host inventory so
                                                          the built-in Zabbix Geomap widget can show them too

What it shows (all from Zabbix, nothing is changed by the page):
  site links    one arrow per site, from the site it hangs off (host "Site SITExxxx", {$SITE.UPSTREAM}) to the site,
               green = main link up (item site.mainlink.up), red = main link down, grey dashed = cut off behind a
               site that is down, amber = LACP trunk lost a member. A site with no "Site" host gets its links from
               the parent tags of its radios (red while a radio on it is unreachable).
  site dots     green OK, amber = devices down or problems at the site, red = Site DOWN / cut off.
  Endpoint dots    green = CPE answers ping, red = disconnected (ping lost), amber = up with a warning (weak signal,
               recent reboot...), grey = not in Zabbix or disabled. Thin line = the site the endpoint belongs to.

Locations come from the two Excel files (embedded below). Settings: NETMAP_URL (address of this server as a
browser sees it, for the dashboard), ZABBIX_WEB_URL (Zabbix web UI, for links and the map library).
"""
import argparse
import glob
import html
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from site_conf import data_file, CORE_SITE  # noqa: E402

TZ = os.environ.get("RCA_TZ", "UTC")
REFRESH = 30          # seconds between page refreshes
STATE_TTL = 20        # seconds the live state is cached (many viewers = one set of API calls)
HOSTS_TTL = 300       # seconds the host list is cached
SITE_PREFIX = "Site "
ENDPOINT_HOSTS = re.compile(os.environ.get("ENDPOINT_SIGNAL_HOSTS") or r"^EP-")
DASHBOARD = "Sites Overview"
GEO_ROWS = 14         # dashboard rows of the map widget (about 70 px each)
LEAFLET_DIRS = ["/usr/share/zabbix/ui", "/usr/share/zabbix", "/var/www/html/zabbix", "/var/www/zabbix",
                "/usr/share/nginx/html/zabbix", "/var/www/html"]
LEAFLET_FILES = {"leaflet.js": ("js/vendors/Leaflet/leaflet.js", "application/javascript; charset=utf-8"),
                 "leaflet.css": ("assets/styles/vendors/Leaflet/leaflet.css", "text/css; charset=utf-8")}
SANDBOX = ["allow-scripts", "allow-same-origin", "allow-popups", "allow-popups-to-escape-sandbox",
           "allow-top-navigation-by-user-activation"]

# SITES_CSV: private site data, kept out of the repository. One row per line, read from
# $MDP_DATA_DIR/site_locations.csv (default: data/site_locations.csv next to tools/). See examples/site_locations.csv.
SITES_CSV = data_file("site_locations.csv")

# ENDPOINTS_CSV: private site data, kept out of the repository. One row per line, read from
# $MDP_DATA_DIR/endpoint_locations.csv (default: data/endpoint_locations.csv next to tools/). See examples/endpoint_locations.csv.
ENDPOINTS_CSV = data_file("endpoint_locations.csv")

# Offline background sketch (rivers, built-up areas, roads as lat/lon lines), optional private data file:
# $MDP_DATA_DIR/map_sketch.json, e.g. {"river": [[[lat, lon], ...]], "urban": [...], "roads": [...]}
SKETCH_JSON = data_file("map_sketch.json") or '{}'

PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Network map</title>
<!--HEAD-->
<style>.leaflet-pane,.leaflet-tile,.leaflet-marker-icon,.leaflet-marker-shadow,.leaflet-tile-container,.leaflet-pane > svg,.leaflet-pane > canvas,.leaflet-zoom-box,.leaflet-image-layer,.leaflet-layer{position: absolute;left: 0;top: 0;} .leaflet-container{overflow: hidden;} .leaflet-tile,.leaflet-marker-icon,.leaflet-marker-shadow{-webkit-user-select: none;-moz-user-select: none;user-select: none;-webkit-user-drag: none;} .leaflet-tile::selection{background: transparent;} .leaflet-safari .leaflet-tile{image-rendering: -webkit-optimize-contrast;} .leaflet-safari .leaflet-tile-container{width: 1600px;height: 1600px;-webkit-transform-origin: 0 0;} .leaflet-marker-icon,.leaflet-marker-shadow{display: block;} .leaflet-container .leaflet-overlay-pane svg,.leaflet-container .leaflet-marker-pane img,.leaflet-container .leaflet-shadow-pane img,.leaflet-container .leaflet-tile-pane img,.leaflet-container img.leaflet-image-layer,.leaflet-container .leaflet-tile{max-width: none !important;max-height: none !important;} .leaflet-container.leaflet-touch-zoom{-ms-touch-action: pan-x pan-y;touch-action: pan-x pan-y;} .leaflet-container.leaflet-touch-drag{-ms-touch-action: pinch-zoom;touch-action: none;touch-action: pinch-zoom;} .leaflet-container.leaflet-touch-drag.leaflet-touch-zoom{-ms-touch-action: none;touch-action: none;} .leaflet-container{-webkit-tap-highlight-color: transparent;} .leaflet-container a{-webkit-tap-highlight-color: rgba(51,181,229,0.4);} .leaflet-tile{filter: inherit;visibility: hidden;} .leaflet-tile-loaded{visibility: inherit;} .leaflet-zoom-box{width: 0;height: 0;-moz-box-sizing: border-box;box-sizing: border-box;z-index: 800;} .leaflet-overlay-pane svg{-moz-user-select: none;} .leaflet-pane{z-index: 400;} .leaflet-tile-pane{z-index: 200;} .leaflet-overlay-pane{z-index: 400;} .leaflet-shadow-pane{z-index: 500;} .leaflet-marker-pane{z-index: 600;} .leaflet-tooltip-pane{z-index: 650;} .leaflet-popup-pane{z-index: 700;} .leaflet-map-pane canvas{z-index: 100;} .leaflet-map-pane svg{z-index: 200;} .leaflet-vml-shape{width: 1px;height: 1px;} .leaflet-control{position: relative;z-index: 800;pointer-events: visiblePainted;pointer-events: auto;} .leaflet-top,.leaflet-bottom{position: absolute;z-index: 1000;pointer-events: none;} .leaflet-top{top: 0;} .leaflet-right{right: 0;} .leaflet-bottom{bottom: 0;} .leaflet-left{left: 0;} .leaflet-control{float: left;clear: both;} .leaflet-right .leaflet-control{float: right;} .leaflet-top .leaflet-control{margin-top: 10px;} .leaflet-bottom .leaflet-control{margin-bottom: 10px;} .leaflet-left .leaflet-control{margin-left: 10px;} .leaflet-right .leaflet-control{margin-right: 10px;} .leaflet-fade-anim .leaflet-tile{will-change: opacity;} .leaflet-fade-anim .leaflet-popup{opacity: 0;-webkit-transition: opacity 0.2s linear;-moz-transition: opacity 0.2s linear;transition: opacity 0.2s linear;} .leaflet-fade-anim .leaflet-map-pane .leaflet-popup{opacity: 1;} .leaflet-zoom-animated{-webkit-transform-origin: 0 0;-ms-transform-origin: 0 0;transform-origin: 0 0;} .leaflet-zoom-anim .leaflet-zoom-animated{will-change: transform;} .leaflet-zoom-anim .leaflet-zoom-animated{-webkit-transition: -webkit-transform 0.25s cubic-bezier(0,0,0.25,1);-moz-transition: -moz-transform 0.25s cubic-bezier(0,0,0.25,1);transition: transform 0.25s cubic-bezier(0,0,0.25,1);} .leaflet-zoom-anim .leaflet-tile,.leaflet-pan-anim .leaflet-tile{-webkit-transition: none;-moz-transition: none;transition: none;} .leaflet-zoom-anim .leaflet-zoom-hide{visibility: hidden;} .leaflet-interactive{cursor: pointer;} .leaflet-grab{cursor: -webkit-grab;cursor: -moz-grab;cursor: grab;} .leaflet-crosshair,.leaflet-crosshair .leaflet-interactive{cursor: crosshair;} .leaflet-popup-pane,.leaflet-control{cursor: auto;} .leaflet-dragging .leaflet-grab,.leaflet-dragging .leaflet-grab .leaflet-interactive,.leaflet-dragging .leaflet-marker-draggable{cursor: move;cursor: -webkit-grabbing;cursor: -moz-grabbing;cursor: grabbing;} .leaflet-marker-icon,.leaflet-marker-shadow,.leaflet-image-layer,.leaflet-pane > svg path,.leaflet-tile-container{pointer-events: none;} .leaflet-marker-icon.leaflet-interactive,.leaflet-image-layer.leaflet-interactive,.leaflet-pane > svg path.leaflet-interactive,svg.leaflet-image-layer.leaflet-interactive path{pointer-events: visiblePainted;pointer-events: auto;} .leaflet-container{background: #ddd;outline: 0;} .leaflet-container a{color: #0078A8;} .leaflet-container a.leaflet-active{outline: 2px solid orange;} .leaflet-zoom-box{border: 2px dotted #38f;background: rgba(255,255,255,0.5);} .leaflet-container{font: 12px/1.5 "Helvetica Neue",Arial,Helvetica,sans-serif;} .leaflet-bar{box-shadow: 0 1px 5px rgba(0,0,0,0.65);border-radius: 4px;} .leaflet-bar a,.leaflet-bar a:hover{background-color: #fff;border-bottom: 1px solid #ccc;width: 26px;height: 26px;line-height: 26px;display: block;text-align: center;text-decoration: none;color: black;} .leaflet-bar a,.leaflet-control-layers-toggle{background-position: 50% 50%;background-repeat: no-repeat;display: block;} .leaflet-bar a:hover{background-color: #f4f4f4;} .leaflet-bar a:first-child{border-top-left-radius: 4px;border-top-right-radius: 4px;} .leaflet-bar a:last-child{border-bottom-left-radius: 4px;border-bottom-right-radius: 4px;border-bottom: none;} .leaflet-bar a.leaflet-disabled{cursor: default;background-color: #f4f4f4;color: #bbb;} .leaflet-touch .leaflet-bar a{width: 30px;height: 30px;line-height: 30px;} .leaflet-touch .leaflet-bar a:first-child{border-top-left-radius: 2px;border-top-right-radius: 2px;} .leaflet-touch .leaflet-bar a:last-child{border-bottom-left-radius: 2px;border-bottom-right-radius: 2px;} .leaflet-control-zoom-in,.leaflet-control-zoom-out{font: bold 18px 'Lucida Console',Monaco,monospace;text-indent: 1px;} .leaflet-touch .leaflet-control-zoom-in,.leaflet-touch .leaflet-control-zoom-out{font-size: 22px;} .leaflet-control-layers{box-shadow: 0 1px 5px rgba(0,0,0,0.4);background: #fff;border-radius: 5px;} .leaflet-touch .leaflet-control-layers-toggle{width: 44px;height: 44px;} .leaflet-control-layers .leaflet-control-layers-list,.leaflet-control-layers-expanded .leaflet-control-layers-toggle{display: none;} .leaflet-control-layers-expanded .leaflet-control-layers-list{display: block;position: relative;} .leaflet-control-layers-expanded{padding: 6px 10px 6px 6px;color: #333;background: #fff;} .leaflet-control-layers-scrollbar{overflow-y: scroll;overflow-x: hidden;padding-right: 5px;} .leaflet-control-layers-selector{margin-top: 2px;position: relative;top: 1px;} .leaflet-control-layers label{display: block;} .leaflet-control-layers-separator{height: 0;border-top: 1px solid #ddd;margin: 5px -10px 5px -6px;} .leaflet-container .leaflet-control-attribution{background: #fff;background: rgba(255,255,255,0.7);margin: 0;} .leaflet-control-attribution,.leaflet-control-scale-line{padding: 0 5px;color: #333;} .leaflet-control-attribution a{text-decoration: none;} .leaflet-control-attribution a:hover{text-decoration: underline;} .leaflet-container .leaflet-control-attribution,.leaflet-container .leaflet-control-scale{font-size: 11px;} .leaflet-left .leaflet-control-scale{margin-left: 5px;} .leaflet-bottom .leaflet-control-scale{margin-bottom: 5px;} .leaflet-control-scale-line{border: 2px solid #777;border-top: none;line-height: 1.1;padding: 2px 5px 1px;font-size: 11px;white-space: nowrap;overflow: hidden;-moz-box-sizing: border-box;box-sizing: border-box;background: #fff;background: rgba(255,255,255,0.5);} .leaflet-control-scale-line:not(:first-child){border-top: 2px solid #777;border-bottom: none;margin-top: -2px;} .leaflet-control-scale-line:not(:first-child):not(:last-child){border-bottom: 2px solid #777;} .leaflet-touch .leaflet-control-attribution,.leaflet-touch .leaflet-control-layers,.leaflet-touch .leaflet-bar{box-shadow: none;} .leaflet-touch .leaflet-control-layers,.leaflet-touch .leaflet-bar{border: 2px solid rgba(0,0,0,0.2);background-clip: padding-box;} .leaflet-popup{position: absolute;text-align: center;margin-bottom: 20px;} .leaflet-popup-content-wrapper{padding: 1px;text-align: left;border-radius: 12px;} .leaflet-popup-content{margin: 13px 19px;line-height: 1.4;} .leaflet-popup-content p{margin: 18px 0;} .leaflet-popup-tip-container{width: 40px;height: 20px;position: absolute;left: 50%;margin-left: -20px;overflow: hidden;pointer-events: none;} .leaflet-popup-tip{width: 17px;height: 17px;padding: 1px;margin: -10px auto 0;-webkit-transform: rotate(45deg);-moz-transform: rotate(45deg);-ms-transform: rotate(45deg);transform: rotate(45deg);} .leaflet-popup-content-wrapper,.leaflet-popup-tip{background: white;color: #333;box-shadow: 0 3px 14px rgba(0,0,0,0.4);} .leaflet-container a.leaflet-popup-close-button{position: absolute;top: 0;right: 0;padding: 4px 4px 0 0;border: none;text-align: center;width: 18px;height: 14px;font: 16px/14px Tahoma,Verdana,sans-serif;color: #c3c3c3;text-decoration: none;font-weight: bold;background: transparent;} .leaflet-container a.leaflet-popup-close-button:hover{color: #999;} .leaflet-popup-scrolled{overflow: auto;border-bottom: 1px solid #ddd;border-top: 1px solid #ddd;} .leaflet-oldie .leaflet-popup-content-wrapper{-ms-zoom: 1;} .leaflet-oldie .leaflet-popup-tip{width: 24px;margin: 0 auto;-ms-filter: "progid:DXImageTransform.Microsoft.Matrix(M11=0.70710678,M12=0.70710678,M21=-0.70710678,M22=0.70710678)";filter: progid:DXImageTransform.Microsoft.Matrix(M11=0.70710678,M12=0.70710678,M21=-0.70710678,M22=0.70710678);} .leaflet-oldie .leaflet-popup-tip-container{margin-top: -1px;} .leaflet-oldie .leaflet-control-zoom,.leaflet-oldie .leaflet-control-layers,.leaflet-oldie .leaflet-popup-content-wrapper,.leaflet-oldie .leaflet-popup-tip{border: 1px solid #999;} .leaflet-div-icon{background: #fff;border: 1px solid #666;} .leaflet-tooltip{position: absolute;padding: 6px;background-color: #fff;border: 1px solid #fff;border-radius: 3px;color: #222;white-space: nowrap;-webkit-user-select: none;-moz-user-select: none;-ms-user-select: none;user-select: none;pointer-events: none;box-shadow: 0 1px 3px rgba(0,0,0,0.4);} .leaflet-tooltip.leaflet-clickable{cursor: pointer;pointer-events: auto;} .leaflet-tooltip-top:before,.leaflet-tooltip-bottom:before,.leaflet-tooltip-left:before,.leaflet-tooltip-right:before{position: absolute;pointer-events: none;border: 6px solid transparent;background: transparent;content: "";} .leaflet-tooltip-bottom{margin-top: 6px;} .leaflet-tooltip-top{margin-top: -6px;} .leaflet-tooltip-bottom:before,.leaflet-tooltip-top:before{left: 50%;margin-left: -6px;} .leaflet-tooltip-top:before{bottom: 0;margin-bottom: -12px;border-top-color: #fff;} .leaflet-tooltip-bottom:before{top: 0;margin-top: -12px;margin-left: -6px;border-bottom-color: #fff;} .leaflet-tooltip-left{margin-left: -6px;} .leaflet-tooltip-right{margin-left: 6px;} .leaflet-tooltip-left:before,.leaflet-tooltip-right:before{top: 50%;margin-top: -6px;} .leaflet-tooltip-left:before{right: 0;margin-right: -12px;border-left-color: #fff;} .leaflet-tooltip-right:before{left: 0;margin-left: -12px;border-right-color: #fff;}</style>
<style>
/* layout: full-window map, floating control panel on the left, legend bottom right */
:root{
 --bg:#eef2f7;--solid:#ffffff;--panel:rgba(255,255,255,.94);--fg:#122033;--muted:#5b6b80;--line:rgba(18,32,51,.12);--chip:#f3f6fa;
 --ok:#16a34a;--warn:#f59e0b;--bad:#e5383b;--cut:#8a96a3;--unk:#9aa6b4;--accent:#1b3270;--flow:rgba(255,255,255,.85);
 --sketch-bg:#e9edf2;--sketch-river:#7fb2e5;--sketch-urban:rgba(27,50,112,.07);--sketch-road:rgba(18,32,51,.22);
 --f-ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--f-num:ui-monospace,"SFMono-Regular",Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
 --bg:#0b1220;--solid:#0e1626;--panel:rgba(14,22,38,.92);--fg:#e6edf7;--muted:#93a3b8;--line:rgba(230,237,247,.12);--chip:rgba(255,255,255,.05);
 --ok:#22c55e;--warn:#fbbf24;--bad:#ff4d4f;--cut:#7b8794;--unk:#64748b;--accent:#8db4ff;--flow:rgba(255,255,255,.9);
 --sketch-bg:#0f1726;--sketch-river:#2f6db3;--sketch-urban:rgba(141,180,255,.07);--sketch-road:rgba(230,237,247,.16);color-scheme:dark}}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:13px/1.4 var(--f-ui);display:flex;flex-direction:column}
body>.mdp-bar{margin:0!important;flex:none}
#map{flex:1;min-height:300px;background:var(--sketch-bg)}
.leaflet-container{font:inherit;background:var(--sketch-bg)}
.nm-panel{position:absolute;z-index:1000;top:12px;left:12px;width:300px;max-height:calc(100% - 24px);display:flex;flex-direction:column;
 background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.28);backdrop-filter:blur(6px);overflow:hidden}
.nm-wrap{position:relative;flex:1;display:flex;min-height:0}
.nm-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line)}
.nm-head h1{font-size:15px;margin:0;font-weight:750;letter-spacing:-.01em}
.nm-live{display:inline-flex;white-space:nowrap;align-items:center;gap:6px;margin-left:auto;font:11.5px var(--f-num);color:var(--muted)}
.nm-live i{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 var(--ok);animation:beat 2s infinite}
.nm-live.err i{background:var(--bad);animation:none}.nm-live.err{color:var(--bad)}
.nm-x{border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:16px;line-height:1;padding:2px 4px}
.nm-body{overflow:auto;padding:10px 12px;display:flex;flex-direction:column;gap:12px}
.nm-kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.nm-kpi{background:var(--chip);border:1px solid var(--line);border-radius:9px;padding:7px 8px}
.nm-kpi b{display:block;font:700 18px/1.1 var(--f-num)}.nm-kpi b small{font-size:11px;color:var(--muted);font-weight:500}
.nm-kpi span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.nm-kpi.bad{border-color:color-mix(in srgb,var(--bad) 55%,transparent)}.nm-kpi.bad b{color:var(--bad)}
.nm-sec{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0 0 6px;font-weight:650}
.nm-att{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
.nm-att li{display:flex;gap:8px;align-items:flex-start;padding:6px 8px;border-radius:8px;cursor:pointer;background:var(--chip);border:1px solid transparent}
.nm-att li:hover,.nm-att li:focus{border-color:var(--line);outline:none}
.nm-att li i{flex:none;width:9px;height:9px;border-radius:50%;margin-top:4px;background:var(--bad)}
.nm-att li.cut i{background:var(--cut)}
.nm-att .ok{color:var(--ok);cursor:default;background:transparent;padding-left:0}
.nm-tog{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px}
.nm-tog label{display:flex;gap:6px;align-items:center;cursor:pointer}
.nm-seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.nm-seg button{flex:1;border:0;background:transparent;color:var(--fg);padding:6px 4px;font:inherit;font-size:12px;cursor:pointer}
.nm-seg button+button{border-left:1px solid var(--line)}
.nm-seg button.on{background:var(--accent);color:#fff}
.nm-search{position:relative}
.nm-search input{width:100%;box-sizing:border-box;font:inherit;padding:7px 9px;border-radius:8px;border:1px solid var(--line);background:var(--chip);color:var(--fg)}
.nm-res{position:absolute;left:0;right:0;top:100%;margin-top:3px;background:var(--panel);border:1px solid var(--line);border-radius:8px;
 list-style:none;padding:3px;z-index:5;box-shadow:0 8px 20px rgba(0,0,0,.25)}
.nm-res li{padding:5px 7px;border-radius:6px;cursor:pointer;display:flex;gap:6px}.nm-res li:hover,.nm-res li.on{background:var(--chip)}
.nm-res small{color:var(--muted);margin-left:auto}
.nm-open{position:absolute;z-index:1000;top:12px;left:12px;border:1px solid var(--line);background:var(--panel);color:var(--fg);
 border-radius:10px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25)}
.nm-open i{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px}
.nm-open.bad i{background:var(--bad)}
.nm-legend{position:absolute;z-index:900;right:10px;bottom:22px;background:var(--panel);border:1px solid var(--line);border-radius:10px;
 padding:8px 10px;display:grid;grid-template-columns:auto auto;gap:3px 12px;font-size:11.5px;box-shadow:0 6px 20px rgba(0,0,0,.2)}
.nm-legend div{display:flex;align-items:center;gap:6px;white-space:nowrap}
.lg-line{width:22px;height:4px;border-radius:2px}.lg-dot{width:9px;height:9px;border-radius:50%;border:1.5px solid rgba(0,0,0,.35)}
.nm-note{position:absolute;z-index:1000;left:50%;transform:translateX(-50%);bottom:24px;background:var(--panel);border:1px solid var(--line);
 border-radius:9px;padding:7px 12px;font-size:12px;box-shadow:0 6px 20px rgba(0,0,0,.25);max-width:80%}
.nm-full{color:var(--accent);text-decoration:none;font-size:12px}
/* sites */
.nm-site{position:relative;display:flex;align-items:center;gap:5px;transform:translate(-9px,-9px);white-space:nowrap;pointer-events:auto}
.nm-site .d{width:14px;height:14px;border-radius:50%;border:2.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.5);flex:none;position:relative}
.nm-site b{font:700 11.5px var(--f-num);color:#fff;background:rgba(11,18,32,.78);padding:1px 5px;border-radius:5px;letter-spacing:.02em}
.nm-site.core .d{width:22px;height:22px;border-width:3px;margin:-4px}
.nm-site.core b{font-size:12.5px;background:#13245e;border:1px solid #8dc63f}
.s-ok .d{background:var(--ok)}.s-warn .d{background:var(--warn)}.s-down .d,.s-cut .d{background:var(--bad)}.s-unknown .d{background:var(--unk)}
.s-cut .d{background:repeating-linear-gradient(45deg,var(--bad) 0 3px,#5b1013 3px 6px)}
.s-down .d:after,.s-cut .d:after{content:"";position:absolute;inset:-9px;border-radius:50%;border:3px solid var(--bad);animation:ring 1.6s ease-out infinite}
.nm-hide-labels .nm-site:not(.core) b{display:none}
.nm-site:hover b{visibility:visible!important;z-index:2}
/* links */
.nm-flow{animation:flow 1.1s linear infinite}
.nm-blink{animation:blink 1s steps(2,start) infinite}
.nm-arrow{pointer-events:none}.nm-arrow svg{display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.55))}
.nm-pdown{animation:ppulse 1.4s ease-out infinite}
.nm-plabel{background:transparent;border:0;box-shadow:none;color:var(--fg);font:600 10.5px var(--f-num);padding:0 0 0 2px;text-shadow:0 0 3px var(--bg),0 0 3px var(--bg)}
.nm-plabel:before{display:none}
.nm-map:not(.z15) .nm-plabel{display:none}
/* popups */
.leaflet-popup-content-wrapper{background:var(--solid);color:var(--fg);border-radius:11px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
.leaflet-popup-tip{background:var(--solid)}
.leaflet-popup-content{margin:12px 14px;min-width:230px;font-size:12.5px}
.leaflet-container a.leaflet-popup-close-button{color:var(--muted)}
.pp h3{margin:0 0 2px;font-size:15px;display:flex;align-items:center;gap:7px}
.pp .st{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:700;color:#fff}
.st.ok,.st.up{background:var(--ok)}.st.warn,.st.degraded{background:var(--warn);color:#2b1d00}.st.down,.st.cut{background:var(--bad)}.st.unknown{background:var(--unk)}
.pp .place{color:var(--muted);direction:rtl;text-align:left;unicode-bidi:plaintext;margin-bottom:6px}
.pp dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:6px 0}
.pp dt{color:var(--muted)}.pp dd{margin:0;font-family:var(--f-num);font-size:12px}
.pp ul{margin:4px 0;padding-left:16px;max-height:130px;overflow:auto}.pp li{margin:1px 0}
.pp .sev{font-weight:700}.sev5{color:var(--bad)}.sev4{color:#f97316}.sev3{color:var(--warn)}.sev2{color:#60a5fa}
.pp .acts{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.pp .acts a{font-size:12px;padding:4px 9px;border-radius:7px;border:1px solid var(--line);text-decoration:none;color:var(--fg);background:var(--chip)}
.pp .acts a.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.good{color:var(--ok)}.meh{color:var(--warn)}.bad{color:var(--bad)}
.leaflet-tooltip.nm-tip{background:var(--panel);color:var(--fg);border:1px solid var(--line);box-shadow:0 4px 14px rgba(0,0,0,.3);font-size:12px}
@keyframes flow{to{stroke-dashoffset:-32}}
@keyframes blink{50%{stroke-opacity:.25}}
@keyframes ring{0%{transform:scale(.5);opacity:1}100%{transform:scale(1.6);opacity:0}}
@keyframes ppulse{0%{stroke-width:2;stroke-opacity:1}100%{stroke-width:14;stroke-opacity:0}}
@keyframes beat{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 60%,transparent)}100%{box-shadow:0 0 0 8px transparent}}
@media (prefers-reduced-motion:reduce){.nm-flow,.nm-blink,.nm-pdown,.nm-live i,.s-down .d:after,.s-cut .d:after{animation:none}}
@media (max-width:640px){.nm-panel{width:auto;right:12px;max-height:45%}.nm-legend{display:none}}
</style></head>
<body class="nm__EMBED__">
<!--BAR-->
<div class="nm-wrap">
 <div id="map" class="nm-map"></div>
 <button class="nm-open" id="open" hidden><i></i>Network</button>
 <aside class="nm-panel" id="panel">
  <div class="nm-head"><h1>Network map</h1>
   <span class="nm-live" id="live"><i></i><span id="clock">loading</span></span>
   <button class="nm-x" id="close" title="Hide the panel" aria-label="Hide the panel">&#x2715;</button></div>
  <div class="nm-body">
   <div class="nm-kpis">
    <div class="nm-kpi" id="k-sites"><span>sites up</span><b>-</b></div>
    <div class="nm-kpi" id="k-links"><span>Links up</span><b>-</b></div>
    <div class="nm-kpi" id="k-endpoints"><span>Endpoints up</span><b>-</b></div>
   </div>
   <div class="nm-search"><input id="q" placeholder="Find site, CPE, endpoint no. or IP" autocomplete="off" aria-label="Search">
    <ul class="nm-res" id="res" hidden></ul></div>
   <div><div class="nm-sec">Needs attention</div><ul class="nm-att" id="att"></ul></div>
   <div><div class="nm-sec">Show</div><div class="nm-tog">
    <label><input type="checkbox" id="t-links" checked> site links</label>
    <label><input type="checkbox" id="t-endpoints" checked> Endpoints</label>
    <label><input type="checkbox" id="t-spokes"> All endpoint lines</label>
    <label><input type="checkbox" id="t-labels" checked> site names</label>
    <label><input type="checkbox" id="t-only"> Only problems</label>
   </div></div>
   <div><div class="nm-sec">Map</div><div class="nm-seg" id="base">
    <button data-b="dark">Dark</button><button data-b="light">Light</button><button data-b="sat">Satellite</button><button data-b="sketch">Offline</button>
   </div></div>
   <a class="nm-full" id="full" href="netmap" target="_blank" rel="noopener">Open full screen &#x2197;</a>
  </div>
 </aside>
 <div class="nm-legend" aria-label="Legend">
  <div><span class="lg-line" style="background:var(--ok)"></span>Link up</div><div><span class="lg-dot" style="background:var(--ok)"></span>Endpoint connected</div>
  <div><span class="lg-line" style="background:var(--bad)"></span>Link down</div><div><span class="lg-dot" style="background:var(--bad)"></span>Endpoint disconnected</div>
  <div><span class="lg-line" style="background:repeating-linear-gradient(90deg,var(--cut) 0 5px,transparent 5px 9px)"></span>Cut off</div><div><span class="lg-dot" style="background:var(--warn)"></span>Endpoint warning</div>
  <div><span class="lg-line" style="background:var(--warn)"></span>LACP degraded</div><div><span class="lg-dot" style="background:var(--unk)"></span>Not monitored</div>
 </div>
 <div class="nm-note" id="note" hidden></div>
</div>
<script>
var CFG=/*CFG*/null;
(function(){
var ROOT=location.pathname.replace(/\/netmap(\/.*)?$/,"")+"/";
var ZBX=CFG.zabbix||(location.protocol+"//"+location.hostname);
var srcs=[ROOT+"netmap/leaflet.js",ZBX+"/js/vendors/Leaflet/leaflet.js","https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"];
function load(i){if(i>=srcs.length){document.getElementById("clock").textContent="map library not found";return}
 var s=document.createElement("script");s.src=srcs[i];s.onload=function(){window.L?start():load(i+1)};s.onerror=function(){load(i+1)};document.head.appendChild(s)}
load(0);

function store(k,v){try{if(v===undefined)return localStorage.getItem("netmap."+k);localStorage.setItem("netmap."+k,v)}catch(e){return null}}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()}
var TGT=CFG.embed?" target=\"_blank\" rel=\"noopener\"":"";

function start(){
 var map=L.map("map",{zoomControl:true,zoomSnap:.25,zoomDelta:.5,wheelPxPerZoomLevel:90,attributionControl:true});
 var dark=matchMedia("(prefers-color-scheme: dark)").matches;
 var BASES={
  dark:L.layerGroup([L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",{maxZoom:19,maxNativeZoom:16,attribution:"&copy; Esri, OpenStreetMap"}),
   L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",{maxZoom:19,maxNativeZoom:16})]),
  light:L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",{maxZoom:19,attribution:"&copy; Esri, OpenStreetMap"}),
  sat:L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{maxZoom:19,attribution:"Imagery &copy; Esri"}),
  sketch:L.layerGroup()};
 var sk=CFG.sketch||{};
 (sk.urban||[]).forEach(function(r){L.polygon(r,{stroke:false,fillColor:css("--sketch-urban"),fillOpacity:1,interactive:false}).addTo(BASES.sketch)});
 (sk.roads||[]).forEach(function(r){L.polyline(r,{color:css("--sketch-road"),weight:1.5,interactive:false}).addTo(BASES.sketch)});
 (sk.river||[]).forEach(function(r){L.polyline(r,{color:css("--sketch-river"),weight:5,opacity:.9,interactive:false}).addTo(BASES.sketch)});
 var cur=null,loaded=0,failed=0;
 function setBase(b){if(!BASES[b])b="dark";if(cur)map.removeLayer(BASES[cur]);cur=b;BASES[b].addTo(map);(BASES[b].getLayers?BASES[b].getLayers():[BASES[b]]).forEach(function(t,i){if(t.bringToBack&&i===0)t.bringToBack()});
  [].forEach.call(document.querySelectorAll("#base button"),function(x){x.classList.toggle("on",x.dataset.b===b)});store("base",b)}
 ["dark","light","sat"].forEach(function(k){(BASES[k].eachLayer?BASES[k].getLayers():[BASES[k]]).forEach(function(t){t.on("tileload",function(){loaded++});t.on("tileerror",function(){failed++})})});
 document.getElementById("base").addEventListener("click",function(e){var b=e.target.dataset&&e.target.dataset.b;if(b){setBase(b);loaded=failed=0;watchTiles()}});
 function watchTiles(){setTimeout(function(){if(cur!=="sketch"&&loaded===0&&failed>0){setBase("sketch");
  note("This PC cannot reach the map tiles (internet): showing the offline sketch. Pick Dark, Light or Satellite to try again.")}},7000)}
 setBase(store("base")||(dark?"dark":"light"));watchTiles();

 var gSpokes=L.layerGroup().addTo(map),gLinks=L.layerGroup().addTo(map),gEndpoints=L.layerGroup().addTo(map),gSites=L.layerGroup().addTo(map);
 var siteL={},linkL={},endpointL={},DATA=null,first=true,SEL=null;
 var COL={ok:"--ok",up:"--ok",warn:"--warn",degraded:"--warn",down:"--bad",cut:"--cut",unknown:"--unk"};
 function col(s){return css(COL[s]||"--unk")}
 function ll(o){return o&&o.lat!=null?[o.lat,o.lon]:null}
 function siteIcon(s){var core=s.code===DATA.core;return L.divIcon({className:"",iconSize:null,
  html:"<div class=\"nm-site s-"+s.state+(core?" core":"")+"\"><span class=\"d\"></span><b>"+esc(s.code)+"</b></div>"})}
 function arrowIcon(deg,c){return L.divIcon({className:"nm-arrow",iconSize:[20,20],iconAnchor:[10,10],
  html:"<svg width=\"20\" height=\"20\" viewBox=\"0 0 18 18\" style=\"transform:rotate("+deg+"deg)\"><path d=\"M3 2 L16 9 L3 16 L6 9 Z\" fill=\""+c+"\" stroke=\"#fff\" stroke-width=\"1.3\" stroke-linejoin=\"round\"/></svg>"})}
 function bearing(a,b){var p=map.project(a,12),q=map.project(b,12);return Math.atan2(q.y-p.y,q.x-p.x)*180/Math.PI}
 function sevName(n){return["Info","Info","Warning","Average","High","Disaster"][n]||""}
 function probs(list,n){if(!list||!list.length)return"";return"<ul>"+list.map(function(p){return"<li><span class=\"sev sev"+p.sev+"\">"+sevName(p.sev)+"</span> "+esc(p.name)+" <span class=\"bad\" style=\"color:var(--muted)\">("+esc(p["for"])+")</span></li>"}).join("")+"</ul>"+(n>list.length?"<div style=\"color:var(--muted)\">and "+(n-list.length)+" more</div>":"")}
 var SL={ok:"OK",warn:"Problems",down:"DOWN",cut:"Cut off",unknown:"No data",up:"Up",degraded:"Degraded"};
 function sitePopup(s){
  var endpoints=DATA.endpoints.filter(function(p){return p.hub===s.code}),pd=endpoints.filter(function(p){return p.state==="down"}).length;
  var zb=s.groupid?ZBX+"/zabbix.php?action=problem.view&filter_set=1&groupids%5B%5D="+s.groupid:"";
  return"<div class=\"pp\"><h3>"+esc(s.code)+(s.code===DATA.core?" (core)":"")+" <span class=\"st "+s.state+"\">"+SL[s.state]+"</span></h3>"
   +(s.place?"<div class=\"place\" dir=\"rtl\">"+esc(s.place)+"</div>":"")+"<div>"+esc(s.why)+"</div><dl>"
   +"<dt>Devices</dt><dd>"+s.devices+" monitored, <span class=\""+(s.down?"bad":"good")+"\">"+s.down+" down</span></dd>"
   +(endpoints.length?"<dt>Endpoints</dt><dd>"+endpoints.length+", <span class=\""+(pd?"bad":"good")+"\">"+pd+" disconnected</span></dd>":"")
   +(s.upstream?"<dt>Fed from</dt><dd>"+esc(s.upstream)+"</dd>":"")+(s.mainlink?"<dt>Main link</dt><dd>"+esc(s.mainlink)+"</dd>":"")
   +"</dl>"+probs(s.problems,s.nproblems)+"<div class=\"acts\">"
   +"<a class=\"pri\" href=\""+ROOT+"rca?site="+encodeURIComponent(s.code)+"&from="+DATA.rca_from+"&to="+DATA.rca_to+"\""+TGT+">RCA last 2 h</a>"
   +(zb?"<a href=\""+zb+"\" target=\"_blank\" rel=\"noopener\">Problems in Zabbix</a>":"")
   +(endpoints.length?"<a href=\""+ROOT+"endpoint-signal/now/daily?site="+encodeURIComponent(s.code)+"\""+TGT+">Endpoint signal</a>":"")+"</div></div>"}
 function sig(v,w,b,u){if(v==null)return"-";return"<span class=\""+(v<b?"bad":v<w?"meh":"good")+"\">"+Math.round(v)+" "+u+"</span>"}
 function endpointPopup(p){
  return"<div class=\"pp\"><h3>"+esc(p.name)+" <span class=\"st "+p.state+"\">"+(p.state==="down"?"Disconnected":p.state==="up"?"Connected":SL[p.state])+"</span></h3>"
   +"<div>"+esc(p.why)+"</div><dl><dt>Endpoint</dt><dd>"+esc(p.endpoint)+"</dd><dt>IP</dt><dd>"+esc(p.ip)+"</dd><dt>site</dt><dd>"+esc(p.hub)+"</dd>"
   +(p.host?"<dt>Signal</dt><dd>"+sig(p.rssi,-70,-75,"dBm")+"</dd><dt>SNR</dt><dd>"+sig(p.snr,20,15,"dB")+"</dd>"
   +(p.ssid?"<dt>Sector</dt><dd>"+esc(p.ssid)+"</dd>":""):"")+"</dl>"+probs(p.problems,(p.problems||[]).length)
   +(p.host?"<div class=\"acts\"><a class=\"pri\" href=\""+ROOT+"logs?host="+encodeURIComponent(p.host)+"\""+TGT+">Device log</a>"
   +"<a href=\""+ROOT+"endpoint-signal/now/daily?host="+encodeURIComponent(p.host)+"\""+TGT+">Signal check</a></div>":"")+"</div>"}
 function linkTip(l){return"<b>"+esc(l.from)+" &#x2192; "+esc(l.to)+"</b> &middot; "+SL[l.state]+(l.km!=null?" &middot; "+l.km+" km":"")+(l.radios?"<br>"+esc(l.radios):"")+(l.main?"":"<br><i>from radio parents (no Site host)</i>")}
 function prad(){var z=map.getZoom();return z<12?3:z<14?4.5:z<15?6:8}
 var only=false;
 function visible(state){return!only||state!=="up"&&state!=="ok"}

 function draw(d){
  DATA=d;var S={};d.sites.forEach(function(s){S[s.code]=s});
  // links
  var seen={};
  d.links.forEach(function(l){var a=ll(S[l.from]),b=ll(S[l.to]);if(!a||!b)return;var id=l.from+">"+l.to;seen[id]=1;var c=col(l.state);
   var o=linkL[id];if(!o){o=linkL[id]={base:L.polyline([a,b],{weight:5,lineCap:"round"}),flow:L.polyline([a,b],{weight:2,dashArray:"3 13",interactive:false,className:"nm-flow"}),
    arrow:L.marker([a[0]+(b[0]-a[0])*.55,a[1]+(b[1]-a[1])*.55],{interactive:false,keyboard:false})};
    o.base.bindTooltip("",{sticky:true,className:"nm-tip"});o.base.addTo(gLinks);o.flow.addTo(gLinks);o.arrow.addTo(gLinks)}
   o.base.setStyle({color:c,opacity:l.state==="cut"?.75:.92,dashArray:l.state==="cut"?"8 9":null});
   var el=o.base.getElement&&o.base.getElement();if(el)el.classList.toggle("nm-blink",l.state==="down");
   o.base.setTooltipContent(linkTip(l));
   o.flow.setStyle({color:css("--flow"),opacity:(l.state==="up"||l.state==="degraded")?.9:0});
   o.arrow.setIcon(arrowIcon(bearing(a,b),c));o.data=l;
   var vis=visible(l.state);[o.base,o.flow,o.arrow].forEach(function(x){vis?gLinks.addLayer(x):gLinks.removeLayer(x)})});
  Object.keys(linkL).forEach(function(id){if(!seen[id]){[linkL[id].base,linkL[id].flow,linkL[id].arrow].forEach(function(x){gLinks.removeLayer(x)});delete linkL[id]}});
  // endpoints and their line to the site
  var r=prad(),showSp=document.getElementById("t-spokes").checked;
  d.endpoints.forEach(function(p){var at=ll(p);if(!at)return;var id=p.name;var c=col(p.state);var o=endpointL[id];
   if(!o){o=endpointL[id]={dot:L.circleMarker(at,{radius:r,weight:1.5,fillOpacity:.95}),spoke:null};
    o.dot.bindPopup("",{maxWidth:320});o.dot.bindTooltip(esc(p.name),{permanent:true,direction:"right",offset:[6,0],className:"nm-plabel"});o.dot.addTo(gEndpoints)}
   var hub=ll(S[p.hub]);
   if(hub){if(!o.spoke){o.spoke=L.polyline([hub,at],{weight:1,interactive:false});o.spoke.addTo(gSpokes)}else o.spoke.setLatLngs([hub,at])}
   o.dot.setStyle({color:p.state==="down"?css("--bad"):"rgba(0,0,0,.55)",fillColor:c,radius:r});
   var el=o.dot.getElement&&o.dot.getElement();if(el)el.classList.toggle("nm-pdown",p.state==="down");
   if(o.spoke)o.spoke.setStyle({color:c,opacity:p.state==="down"||p.hub===SEL?.8:.3,weight:p.hub===SEL?1.6:1,dashArray:p.state==="unknown"?"2 4":null});
   o.dot.setPopupContent(endpointPopup(p));o.data=p;
   var vis=visible(p.state);vis?gEndpoints.addLayer(o.dot):gEndpoints.removeLayer(o.dot);
   if(o.spoke)(vis&&(showSp||p.hub===SEL||p.state==="down"))?gSpokes.addLayer(o.spoke):gSpokes.removeLayer(o.spoke);
   if(p.state==="down"&&vis)o.dot.bringToFront()});
  // sites on top
  d.sites.forEach(function(s){var at=ll(s);if(!at)return;var o=siteL[s.code];
   if(!o){o=siteL[s.code]={m:L.marker(at,{riseOnHover:true,zIndexOffset:s.code===d.core?1000:0})};o.m.bindPopup("",{maxWidth:340});o.m.addTo(gSites);(function(code,m){m.on("popupopen",function(){SEL=code;draw(DATA)});m.on("popupclose",function(){if(SEL===code){SEL=null;draw(DATA)}})})(s.code,o.m)}
   o.m.setIcon(siteIcon(s));o.m.setPopupContent(sitePopup(s));o.data=s;
   if(s.state==="down"||s.state==="cut")o.m.setZIndexOffset(900)});
  panel(d);declutter();
  if(first){first=false;var pts=d.sites.filter(ll).map(ll).concat(d.endpoints.filter(ll).map(ll));
   if(pts.length)map.fitBounds(pts,{paddingTopLeft:[document.getElementById("panel").hidden?20:330,20],paddingBottomRight:[20,20]})}
 }

 // site names: the most important first, a name that would overlap one already shown waits for zoom or hover
 var PRI={down:0,cut:0,warn:1,unknown:3,ok:2};
 function declutter(){if(!DATA)return;var placed=[],dots=[];
  Object.keys(siteL).forEach(function(k){var e=siteL[k].m.getElement();if(e)dots.push({k:k,r:e.querySelector(".d").getBoundingClientRect()})});
  DATA.sites.slice().sort(function(a,b){return(a.code===DATA.core?-1:PRI[a.state])-(b.code===DATA.core?-1:PRI[b.state])}).forEach(function(s){
   var o=siteL[s.code];if(!o||!o.m.getElement())return;var b=o.m.getElement().querySelector("b");b.style.visibility="";
   var r=b.getBoundingClientRect(),hit=placed.concat(dots.filter(function(x){return x.k!==s.code}).map(function(x){return x.r})).some(function(p){return r.left<p.right+2&&r.right>p.left-2&&r.top<p.bottom+1&&r.bottom>p.top-1});
   if(hit)b.style.visibility="hidden";else placed.push(r)})}
 function panel(d){
  var sUp=d.sites.filter(function(s){return s.state==="ok"||s.state==="warn"}).length,sBad=d.sites.filter(function(s){return s.state==="down"||s.state==="cut"}).length;
  var lUp=d.links.filter(function(l){return l.state==="up"||l.state==="degraded"}).length;
  var known=d.endpoints.filter(function(p){return p.state!=="unknown"}),pUp=known.filter(function(p){return p.state!=="down"}).length;
  kpi("k-sites",sUp,d.sites.length,sBad>0);kpi("k-links",lUp,d.links.length,lUp<d.links.length);kpi("k-endpoints",pUp,known.length,pUp<known.length);
  var ul=document.getElementById("att");
  ul.innerHTML=d.attention.length?d.attention.map(function(a,i){return"<li tabindex=\"0\" data-i=\""+i+"\" class=\""+a.state+"\"><i></i><span>"+esc(a.text)+"</span></li>"}).join("")
   :"<li class=\"ok\">Everything on the map is up.</li>";
  var o=document.getElementById("open");o.classList.toggle("bad",d.attention.length>0);
  o.lastChild.textContent=d.attention.length?d.attention.length+" need attention":"Network OK"}
 function kpi(id,up,all,bad){var e=document.getElementById(id);e.classList.toggle("bad",!!bad);e.querySelector("b").innerHTML=up+"<small>/"+all+"</small>"}

 function focus(a){if(a.kind==="site"&&siteL[a.id]){map.once("moveend",function(){siteL[a.id].m.openPopup()});map.flyTo(siteL[a.id].m.getLatLng(),14,{duration:.8})}
  else if(a.kind==="link"&&linkL[a.id]){map.flyToBounds(linkL[a.id].base.getBounds(),{padding:[80,80],duration:.8})}
  else if(a.kind==="endpoints"){var pts=DATA.endpoints.filter(function(p){return p.hub===a.id&&p.state==="down"&&ll(p)}).map(ll);
   var s=DATA.sites.filter(function(s){return s.code===a.id&&ll(s)});if(s.length)pts.push(ll(s[0]));if(pts.length)map.flyToBounds(pts,{padding:[60,60],maxZoom:16,duration:.8})}}
 document.getElementById("att").addEventListener("click",function(e){var li=e.target.closest("li[data-i]");if(li)focus(DATA.attention[+li.dataset.i])});
 document.getElementById("att").addEventListener("keydown",function(e){if(e.key==="Enter"){var li=e.target.closest("li[data-i]");if(li)focus(DATA.attention[+li.dataset.i])}});

 // search
 var q=document.getElementById("q"),res=document.getElementById("res"),hits=[];
 q.addEventListener("input",function(){var t=q.value.trim().toLowerCase();hits=[];if(!t||!DATA){res.hidden=true;return}
  DATA.sites.forEach(function(s){if(s.code.toLowerCase().indexOf(t)>=0||(s.place||"").indexOf(q.value.trim())>=0)hits.push({t:"site",id:s.code,l:s.code,s:s.state})});
  DATA.endpoints.forEach(function(p){if(p.name.toLowerCase().indexOf(t)>=0||p.ip.indexOf(t)===0||String(p.endpoint)===t.replace(/^p0*/,"")||(p.host||"").toLowerCase().indexOf(t)>=0)hits.push({t:"endpoint",id:p.name,l:p.name+" &middot; endpoint "+esc(p.endpoint),s:p.state})});
  hits=hits.slice(0,8);res.innerHTML=hits.map(function(h,i){return"<li data-i=\""+i+"\"><span>"+(h.t==="site"?esc(h.l):h.l)+"</span><small>"+(h.t==="site"?"site":SL[h.s]||"")+"</small></li>"}).join("")||"<li>No match</li>";res.hidden=false});
 function go(h){res.hidden=true;if(!h)return;if(h.t==="site"){focus({kind:"site",id:h.id})}else{var o=endpointL[h.id];if(!o)return;
  if(!gEndpoints.hasLayer(o.dot))gEndpoints.addLayer(o.dot);map.once("moveend",function(){o.dot.openPopup()});map.flyTo(o.dot.getLatLng(),16,{duration:.8})}}
 res.addEventListener("click",function(e){var li=e.target.closest("li[data-i]");if(li)go(hits[+li.dataset.i])});
 q.addEventListener("keydown",function(e){if(e.key==="Enter")go(hits[0]);if(e.key==="Escape"){res.hidden=true;q.value=""}});

 // toggles
 function tog(id,fn){var e=document.getElementById(id);var s=store(id);if(s!==null)e.checked=s==="1";e.addEventListener("change",function(){store(id,e.checked?"1":"0");fn(e.checked)});fn(e.checked)}
 tog("t-links",function(v){v?map.addLayer(gLinks):map.removeLayer(gLinks)});
 tog("t-endpoints",function(v){v?map.addLayer(gEndpoints):map.removeLayer(gEndpoints)});
 tog("t-spokes",function(){if(DATA)draw(DATA)});
 tog("t-labels",function(v){document.getElementById("map").classList.toggle("nm-hide-labels",!v)});
 tog("t-only",function(v){only=v;if(DATA)draw(DATA)});
 map.on("zoomend",function(){var z=map.getZoom(),r=prad();document.getElementById("map").classList.toggle("z15",z>=15);
  Object.keys(endpointL).forEach(function(k){endpointL[k].dot.setRadius(r)});if(DATA)draw(DATA)});
 map.on("moveend",declutter);
 // panel
 var P=document.getElementById("panel"),O=document.getElementById("open");
 function showPanel(v){P.hidden=!v;O.hidden=v;store("panel",v?"1":"0")}
 document.getElementById("close").onclick=function(){showPanel(false)};O.onclick=function(){showPanel(true)};
 showPanel(store("panel")!=="0");
 document.getElementById("full").href=ROOT+"netmap";if(!CFG.embed)document.getElementById("full").hidden=true;

 var live=document.getElementById("live"),clock=document.getElementById("clock"),last=null;
 function refresh(){fetch(ROOT+"netmap/data",{credentials:"same-origin",cache:"no-store"}).then(function(r){return r.json().then(function(j){if(!r.ok||j.error)throw new Error(j.error||r.status);return j})})
  .then(function(d){draw(d);last=d.clock;live.classList.remove("err");clock.textContent="live "+d.clock})
  .catch(function(e){live.classList.add("err");clock.textContent=last?"no update since "+last:"cannot read Zabbix";console.warn(e)})
  .then(function(){setTimeout(refresh,((DATA&&DATA.refresh)||30)*1000)})}
 refresh();
}
var nt;function note(t){var n=document.getElementById("note");n.textContent=t;n.hidden=false;clearTimeout(nt);nt=setTimeout(function(){n.hidden=true},12000)}
})();
</script>
</body></html>
'''


def _sites():
    out = {}
    for line in SITES_CSV.strip().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        code, lat, lon, place = (line.split(",", 3) + [""])[:4]
        out[code.strip().upper()] = {"lat": float(lat), "lon": float(lon), "place": place.strip()}
    return out


def _endpoints():
    out = []
    for line in ENDPOINTS_CSV.strip().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) != 6:
            continue
        name, lat, lon, ip, endpoint, hub = p
        out.append({"name": name, "lat": float(lat) if lat else None, "lon": float(lon) if lon else None,
                    "ip": ip, "endpoint": endpoint, "hub": hub.upper()})
    return out


SITES = _sites()
ENDPOINTS = _endpoints()
SKETCH = json.loads(SKETCH_JSON)


def km(a, b):
    """Great-circle distance in km between two (lat, lon)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(12742 * math.asin(math.sqrt(h)), 1)


# --------------------------------------------------------------------------- reading Zabbix
_cache = {"hosts": (0, None), "state": (0, None)}
_lock = threading.Lock()


def _hosts(api):
    """Every host with its tags, IP, macros (Site hosts) and site group, cached for HOSTS_TTL."""
    t, v = _cache["hosts"]
    if v is not None and time.time() - t < HOSTS_TTL:
        return v
    raw = api.call("host.get", {"output": ["hostid", "host", "name", "status"], "selectTags": "extend",
                                "selectInterfaces": ["ip", "main", "type"], "selectMacros": ["macro", "value"]})
    groups = {g["name"].split("/", 1)[1]: g["groupid"] for g in api.call("hostgroup.get", {
        "output": ["groupid", "name"], "search": {"name": "Sites/"}, "startSearch": True})}
    hosts = {}
    for h in raw:
        tags = {x["tag"]: x["value"] for x in h.get("tags", [])}
        ifs = sorted(h.get("interfaces") or [], key=lambda i: (str(i.get("main")) != "1", str(i.get("type")) != "2"))
        hosts[h["hostid"]] = {"hostid": h["hostid"], "host": h["host"], "name": h["name"],
                              "disabled": str(h.get("status")) == "1", "tags": tags,
                              "ip": ifs[0]["ip"] if ifs else "",
                              "macros": {m["macro"]: m.get("value", "") for m in h.get("macros") or []}}
    v = {"hosts": hosts, "groups": groups}
    _cache["hosts"] = (time.time(), v)
    return v


def _local(epoch):
    return datetime.fromtimestamp(epoch, ZoneInfo(TZ)).strftime("%Y-%m-%dT%H:%M")


def _ago(epoch):
    s = max(0, int(time.time() - int(epoch)))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def state(api):
    """The live picture as a dict (cached STATE_TTL seconds)."""
    with _lock:
        t, v = _cache["state"]
        if v is not None and time.time() - t < STATE_TTL:
            return v
        v = _build(api)
        _cache["state"] = (time.time(), v)
        return v


def _build(api):
    hv = _hosts(api)
    hosts, groups = hv["hosts"], hv["groups"]
    live = {i: h for i, h in hosts.items() if not h["disabled"]}
    site_hosts = {h["host"][len(SITE_PREFIX):]: h for h in live.values()
                  if h["host"].startswith(SITE_PREFIX) and h["tags"].get("class") == "site"}
    endpoints_z = {i: h for i, h in hosts.items() if ENDPOINT_HOSTS.search(h["host"])}

    keys = ["icmpping", "site.mainlink.up", "site.devices", "site.devices.down", "site.upstream.down",
            "wifi.rssi", "wifi.snr", "wifi.link.ssid"]
    items = api.call("item.get", {"output": ["hostid", "key_", "lastvalue", "lastclock"], "monitored": True,
                                  "filter": {"key_": keys}}) if live else []
    val = {}
    for i in items:
        if i.get("lastclock") and str(i["lastclock"]) != "0":
            val[(i["hostid"], i["key_"])] = i["lastvalue"]
    trig = api.call("trigger.get", {"output": ["triggerid", "description", "priority", "lastchange"],
                                    "filter": {"value": 1}, "monitored": True, "expandDescription": True,
                                    "selectHosts": ["hostid"], "selectTags": "extend"})
    probs = {}
    for t in trig:
        tags = {x["tag"]: x["value"] for x in t.get("tags", [])}
        for h in t["hosts"]:
            probs.setdefault(h["hostid"], []).append({"name": t["description"], "sev": int(t["priority"]),
                                                     "since": int(t["lastchange"]), "alarm": tags.get("alarm", "")})

    def num(hid, key):
        try:
            return float(val[(hid, key)])
        except (KeyError, ValueError):
            return None

    # ---- sites
    by_site = {}
    for h in live.values():
        s = h["tags"].get("site")
        if s:
            by_site.setdefault(s, []).append(h)
    codes = sorted(set(SITES) | set(groups) | set(site_hosts))
    now = time.time()
    sites, attention = [], []
    for c in codes:
        loc = SITES.get(c)
        devs = by_site.get(c, [])
        sh = site_hosts.get(c)
        total = num(sh["hostid"], "site.devices") if sh else None
        down = num(sh["hostid"], "site.devices.down") if sh else None
        if total is None:
            pings = [num(h["hostid"], "icmpping") for h in devs if not ENDPOINT_HOSTS.search(h["host"])]
            total = sum(1 for p in pings if p is not None)
            down = sum(1 for p in pings if p == 0)
        plist = sorted((p for h in devs if not ENDPOINT_HOSTS.search(h["host"]) for p in probs.get(h["hostid"], [])),
                       key=lambda p: (-p["sev"], -p["since"]))   # endpoints have their own dots
        sp = probs.get(sh["hostid"], []) if sh else []
        sdown = next((p for p in sp if p["alarm"] == "site-down"), None)
        scut = next((p for p in sp if p["alarm"] == "site-cutoff"), None)
        if sdown:
            st, why = "down", f"Site DOWN for {_ago(sdown['since'])}"
        elif scut:
            st, why = "cut", f"Cut off: {sh['macros'].get('{$SITE.UPSTREAM}', 'upstream')} is down ({_ago(scut['since'])})"
        elif (down or 0) > 0 or any(p["sev"] >= 3 for p in plist):
            st, why = "warn", f"{int(down or 0)} device(s) down" if down else "Problems at the site"
        elif total:
            st, why = "ok", "All devices answer"
        else:
            st, why = "unknown", "No monitored devices"
        sites.append({"code": c, "lat": loc["lat"] if loc else None, "lon": loc["lon"] if loc else None,
                      "place": loc["place"] if loc else "", "state": st, "why": why,
                      "devices": int(total or 0), "down": int(down or 0), "groupid": groups.get(c),
                      "mainlink": sh["macros"].get("{$SITE.MAINLINK}", "") if sh else "",
                      "upstream": sh["macros"].get("{$SITE.UPSTREAM}", "") if sh else "",
                      "problems": [{"name": p["name"], "sev": p["sev"], "for": _ago(p["since"])} for p in plist[:8]],
                      "nproblems": len(plist)})
        if st in ("down", "cut"):
            attention.append({"kind": "site", "id": c, "state": st, "text": f"{c} {why}"})
    site_state = {s["code"]: s["state"] for s in sites}

    # ---- site to site links: the main link of every site from its Site host, else from radio parent tags
    links, have = [], set()
    for c, sh in sorted(site_hosts.items()):
        up = sh["macros"].get("{$SITE.UPSTREAM}", "").strip().upper()
        if not up or up == c:
            continue
        mu = num(sh["hostid"], "site.mainlink.up")
        ml = sh["macros"].get("{$SITE.MAINLINK}", "")
        lacp = ("LACP" in ml or "Trunk" in ml) and any(
            p["alarm"] == "lacp" and (re.search(r"Trunk\s*\d+", p["name"]) is None or re.search(r"Trunk\s*\d+", p["name"]).group(0).replace(" ", "") in ml)
            for h in by_site.get(c, []) + by_site.get(up, []) for p in probs.get(h["hostid"], []))
        if mu is None:
            st = "unknown"
        elif mu >= 1:
            st = "degraded" if lacp else "up"
        elif site_state.get(up) in ("down", "cut") or num(sh["hostid"], "site.upstream.down"):
            st = "cut"
        else:
            st = "down"
        links.append({"from": up, "to": c, "state": st, "radios": sh["macros"].get("{$SITE.MAINLINK}", ""),
                      "main": True})
        have.add(c)
    by_name = {h["host"]: h for h in live.values()}
    extra = {}
    for h in live.values():
        p = by_name.get(h["tags"].get("parent", ""))
        a, b = (p or {}).get("tags", {}).get("site"), h["tags"].get("site")
        if not p or not a or not b or a == b or b in have or ENDPOINT_HOSTS.search(h["host"]):
            continue
        e = extra.setdefault((a, b), {"radios": set(), "down": False})
        e["radios"].update([p["host"], h["host"]])
        e["down"] |= any(x["alarm"] == "unreachable" for x in probs.get(h["hostid"], []) + probs.get(p["hostid"], []))
    for (a, b), e in sorted(extra.items()):
        st = "cut" if e["down"] and site_state.get(a) in ("down", "cut") else "down" if e["down"] else "up"
        links.append({"from": a, "to": b, "state": st, "radios": " ~ ".join(sorted(e["radios"])), "main": False})
    for ln in links:
        a, b = SITES.get(ln["from"]), SITES.get(ln["to"])
        ln["km"] = km((a["lat"], a["lon"]), (b["lat"], b["lon"])) if a and b else None
        if ln["state"] == "down":
            attention.append({"kind": "link", "id": f"{ln['from']}>{ln['to']}", "state": "down",
                              "text": f"Link {ln['from']} to {ln['to']} down"})

    # ---- endpoints: Excel row matched to its Zabbix host by IP, else by visible name
    by_ip = {h["ip"]: h for h in endpoints_z.values() if h["ip"]}
    by_vis = {h["name"].strip().lower(): h for h in endpoints_z.values()}
    endpoints, used = [], set()
    for p in ENDPOINTS:
        h = by_ip.get(p["ip"]) or by_vis.get(p["name"].lower())
        row = {"name": p["name"], "endpoint": p["endpoint"], "ip": p["ip"], "hub": p["hub"], "lat": p["lat"], "lon": p["lon"]}
        if not h:
            row.update(state="unknown", why="Not in Zabbix")
        else:
            used.add(h["hostid"])
            ping = num(h["hostid"], "icmpping")
            pr = [x for x in probs.get(h["hostid"], [])]
            row.update(host=h["host"], hub=h["tags"].get("site") or p["hub"],
                       rssi=num(h["hostid"], "wifi.rssi"), snr=num(h["hostid"], "wifi.snr"),
                       ssid=val.get((h["hostid"], "wifi.link.ssid"), ""),
                       problems=[{"name": x["name"], "sev": x["sev"], "for": _ago(x["since"])} for x in pr][:5])
            if h["disabled"]:
                row.update(state="unknown", why="Disabled in Zabbix")
            elif ping == 0:
                u = next((x for x in pr if x["alarm"] == "unreachable"), None)
                row.update(state="down", why="Disconnected" + (f" for {_ago(u['since'])}" if u else " (no ping)"))
            elif ping is None:
                row.update(state="unknown", why="No data yet")
            elif any(x["sev"] >= 2 for x in pr):
                row.update(state="warn", why=max(pr, key=lambda x: x["sev"])["name"])
            else:
                row.update(state="up", why="Connected")
        endpoints.append(row)
    nogeo = sorted(h["name"] for i, h in endpoints_z.items() if i not in used and not h["disabled"])
    down_by = {}
    for p in endpoints:
        if p["state"] == "down":
            down_by.setdefault(p["hub"], []).append(p["name"])
    for hub, names in sorted(down_by.items()):
        if site_state.get(hub) in ("down", "cut"):
            continue   # the site line already says it
        attention.append({"kind": "endpoints", "id": hub, "state": "down",
                          "text": f"{hub}: {len(names)} endpoint{'s' if len(names) > 1 else ''} down ({', '.join(names[:4])}"
                                  f"{', ...' if len(names) > 4 else ''})"})
    rank = {"site": 0, "link": 1, "endpoints": 2}
    attention.sort(key=lambda a: (rank[a["kind"]], a["id"]))
    return {"time": int(now), "clock": datetime.fromtimestamp(now, ZoneInfo(TZ)).strftime("%H:%M:%S"),
            "rca_from": _local(now - 2 * 3600), "rca_to": _local(now), "refresh": REFRESH,
            "sites": sites, "links": links, "endpoints": endpoints, "attention": attention, "nogeo": nogeo,
            "core": CORE_SITE}


# --------------------------------------------------------------------------- web
def leaflet(name, web_base=""):
    """(content type, text) of Zabbix's own copy of the map library, or None."""
    rel, ctype = LEAFLET_FILES[name]
    cands = [os.path.join(d, rel) for d in LEAFLET_DIRS] + glob.glob(f"/usr/share/zabbix*/**/{os.path.basename(rel)}",
                                                                    recursive=True)
    for f in cands:
        if os.path.isfile(f) and "eaflet" in f:
            return ctype, open(f, encoding="utf-8", errors="replace").read()
    return None


def web(api, parts, q, zabbix_web="", bar="", head=""):
    """(code, content type, body) for /netmap, /netmap/data, /netmap/leaflet.js|css."""
    if parts == ["data"]:
        try:
            return 200, "application/json", json.dumps(state(api), ensure_ascii=False, separators=(",", ":"))
        except Exception as ex:  # noqa: BLE001 - the page shows it and keeps the last picture
            return 502, "application/json", json.dumps({"error": str(ex)})
    if len(parts) == 1 and parts[0] in LEAFLET_FILES:
        hit = leaflet(parts[0])
        if hit:
            return 200, hit[0], hit[1]
        return 404, "text/plain", "not found"
    return 200, "text/html; charset=utf-8", page(zabbix_web, q.get("embed") == "1", bar, head)


def page(zabbix_web="", embed=False, bar_html="", head_html=""):
    cfg = {"zabbix": zabbix_web.rstrip("/"), "embed": embed, "sketch": SKETCH}
    return (PAGE.replace("/*CFG*/null", json.dumps(cfg, ensure_ascii=False, separators=(",", ":")))
            .replace("<!--BAR-->", "" if embed else bar_html).replace("<!--HEAD-->", head_html)
            .replace("__EMBED__", " embed" if embed else ""))


# --------------------------------------------------------------------------- Zabbix side
def server_url(api):
    """Where browsers reach MDataPulse: NETMAP_URL, else the address in the 'Health reports' menu entry."""
    if os.environ.get("NETMAP_URL"):
        return os.environ["NETMAP_URL"].rstrip("/")
    for s in api.call("script.get", {"output": ["name", "url"], "filter": {"type": 6}}):
        m = re.match(r"(https?://[^/]+(?:/[^?]*?)?)/(reports|signal|endpoint-signal|rca|logs)\b", s.get("url") or "")
        if m:
            return m.group(1)
    return None


def widget(api, x=0, y=0, width=72, height=GEO_ROWS):
    """The Zabbix URL widget that shows the map, or None when the server address is not known."""
    base = server_url(api)
    if not base:
        return None
    return {"type": "url", "name": "Network map: green = up, red = down (click a dot for details)",
            "x": x, "y": y, "width": width, "height": height, "view_mode": 0,
            "fields": [{"type": 1, "name": "url", "value": base + "/netmap?embed=1"}]}


def allow_frame(api, do_apply):
    """The URL widget runs pages in a sandboxed frame: let it run scripts and open links in a new tab."""
    s = api.call("settings.get", {"output": ["iframe_sandboxing_enabled", "iframe_sandboxing_exceptions"]})
    if str(s.get("iframe_sandboxing_enabled")) != "1":
        print("  ok      frame sandboxing is off: nothing to change")
        return
    have = (s.get("iframe_sandboxing_exceptions") or "").split()
    need = [x for x in SANDBOX if x not in have]
    if not need:
        print("  ok      frame sandbox already allows scripts")
        return
    print(("  APPLY " if do_apply else "  would ") + f" allow in the URL widget frame: {' '.join(need)} "
          "(Administration > General > Security)")
    if do_apply:
        api.call("settings.update", {"iframe_sandboxing_exceptions": " ".join(have + need)})


def cmd_dashboard(api, name, do_apply, url=None):
    if url:
        os.environ["NETMAP_URL"] = url
    w = widget(api)
    if not w:
        sys.exit("Cannot tell where MDataPulse runs: add --url http://SERVER:8081")
    print(f"Map address: {w['fields'][0]['value']}")
    allow_frame(api, do_apply)
    d = api.call("dashboard.get", {"output": "extend", "selectPages": "extend", "filter": {"name": [name]}})
    if not d:
        sys.exit(f"No dashboard named {name!r}.")
    d = d[0]
    page_ = next((p for p in d["pages"] if p.get("name") == "Network"), d["pages"][0])
    ws = page_["widgets"]
    old = next((x for x in ws if x["type"] == "map"), None) or \
        next((x for x in ws if x["type"] == "url" and "/netmap" in json.dumps(x.get("fields"))), None)
    if not old:
        sys.exit(f"No map widget on the first page of {name!r}: nothing to replace.")
    ox, oy, ow, oh = (int(old[k]) for k in ("x", "y", "width", "height"))
    grow = GEO_ROWS - oh
    print(("  APPLY " if do_apply else "  would ") + f" replace the '{old.get('name') or old['type']}' widget "
          f"({ow}x{oh}) with the geo map ({ow}x{GEO_ROWS}); {len(ws) - 1} other widgets "
          + (f"move down {grow} rows" if grow > 0 else "stay"))
    if not do_apply:
        return
    out = []
    for x in ws:
        if x is old:
            nw = widget(api, ox, oy, ow, GEO_ROWS)
            nw["widgetid"] = old["widgetid"] if old["type"] == "url" else None
            out.append({k: v for k, v in nw.items() if v is not None})
            continue
        x = dict(x)
        if grow > 0 and int(x["y"]) >= oy + oh:
            x["y"] = int(x["y"]) + grow
        out.append(x)
    if max(int(x["y"]) + int(x["height"]) for x in out) > 64:
        sys.exit("NOT CHANGED: the page would pass row 64 (Zabbix limit). Lower GEO_ROWS.")
    pages = [{"dashboard_pageid": p["dashboard_pageid"], "name": p["name"], "display_period": p["display_period"],
              "widgets": out if p is page_ else p["widgets"]} for p in d["pages"]]
    api.call("dashboard.update", {"dashboardid": d["dashboardid"], "pages": pages})
    print(f"Done: open {name!r} in Zabbix.")


def cmd_inventory(api, do_apply):
    """Optional: location_lat / location_lon in the host inventory of every Site host and endpoint CPE."""
    hv = _hosts(api)
    want = {}
    for h in hv["hosts"].values():
        if h["host"].startswith(SITE_PREFIX) and h["host"][len(SITE_PREFIX):] in SITES:
            s = SITES[h["host"][len(SITE_PREFIX):]]
            want[h["hostid"]] = (s["lat"], s["lon"], s["place"])
    by_ip = {h["ip"]: h for h in hv["hosts"].values() if ENDPOINT_HOSTS.search(h["host"]) and h["ip"]}
    for p in ENDPOINTS:
        h = by_ip.get(p["ip"])
        if h and p["lat"] is not None:
            want[h["hostid"]] = (p["lat"], p["lon"], f"Endpoint {p['endpoint']}, {p['hub']}")
    have = {h["hostid"]: h for h in api.call("host.get", {"output": ["hostid", "host", "inventory_mode"],
                                                        "hostids": list(want), "selectInventory": [
                                                            "location_lat", "location_lon"]})} if want else {}
    n = 0
    for hid, (lat, lon, where) in sorted(want.items()):
        h = have.get(hid)
        if not h:
            continue
        inv = h.get("inventory") or {}
        if isinstance(inv, dict) and inv.get("location_lat") == f"{lat:.6f}" and inv.get("location_lon") == f"{lon:.6f}":
            continue
        n += 1
        if do_apply:
            body = {"hostid": hid, "inventory": {"location_lat": f"{lat:.6f}", "location_lon": f"{lon:.6f}",
                                                 "location": where[:128]}}
            if str(h.get("inventory_mode")) == "-1":
                body["inventory_mode"] = 0
            api.call("host.update", body)
    print(("APPLIED" if do_apply else "Would set") + f" the location of {n} host(s) ({len(want)} have one).")


def cmd_check(api):
    st = _build(api)
    places = [s for s in st["sites"] if s["lat"] is None]
    print(f"sites: {len(st['sites'])} ({len(st['sites']) - len(places)} with a location)")
    for s in places:
        print(f"  no location in the Excel: {s['code']}")
    print(f"site links: {len(st['links'])} ({sum(1 for x in st['links'] if x['main'])} main links from the Site hosts)")
    for x in st["links"]:
        print(f"  {x['from']} -> {x['to']:8} {x['state']:8} {x['km'] or '?'} km  {x['radios']}")
    cnt = {}
    for p in st["endpoints"]:
        cnt[p["state"]] = cnt.get(p["state"], 0) + 1
    print(f"Endpoints: {len(st['endpoints'])} in the Excel: " + ", ".join(f"{v} {k}" for k, v in sorted(cnt.items())))
    for p in st["endpoints"]:
        if p["lat"] is None:
            print(f"  no coordinates in the Excel: {p['name']} ({p['ip']})")
        if p.get("why") == "Not in Zabbix":
            print(f"  not in Zabbix: {p['name']} ({p['ip']})")
    if st["nogeo"]:
        print(f"Endpoint CPEs in Zabbix with no row in the Excel ({len(st['nogeo'])}): {', '.join(st['nogeo'][:30])}")
    print(f"Leaflet from this server: {'found' if leaflet('leaflet.js') else 'NOT found (the page loads it from Zabbix or the internet)'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("dashboard")
    d.add_argument("--name", default=DASHBOARD)
    d.add_argument("--url", help="MDataPulse address as a browser sees it, e.g. http://10.20.100.11:8081")
    d.add_argument("--apply", action="store_true")
    i = sub.add_parser("inventory")
    i.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    from zbxapi import ZabbixAPI
    api = ZabbixAPI()
    if a.cmd == "check":
        cmd_check(api)
    elif a.cmd == "dashboard":
        cmd_dashboard(api, a.name, a.apply, a.url)
    else:
        cmd_inventory(api, a.apply)


if __name__ == "__main__":
    main()
