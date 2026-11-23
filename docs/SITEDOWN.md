# Site down alarm

When the main link of a site is lost you get **one** problem instead of a list of "Device unreachable" lines:

    Site SITE0002 DOWN: main link SITE0001-WAVE-1 ~ SITE0002-WAVE-2 lost, 5 of 5 devices down. Also cut off: SITE0003

It is raised on a small status host per site called **Site SITExxxx** (group `Sites/SITExxxx`, so it shows on that
site's dashboard) and clears by itself when the main link answers again.

## How it decides

* The main link of every site is listed in `data/main_links.txt` (private, not in the repository; see
  `examples/main_links.txt` for the format).
* **Site down** = no far-end main-link radio answered ping for `{$SITE.DOWN.TIME}` (2m) **and** at least
  `{$SITE.DOWN.PCT}` (80%) of the site's devices are not answering. Two radios joined by `+` (an LACP pair) count
  as down only when both are.
* 2m is shorter than the device alarm (3m), so the site alarm comes first and the "Device unreachable" alarms of the
  site wait on it (trigger dependency). The Site down of a site behind it waits on it too, and shows as
  "DOWN via upstream". An event correlation rule closes any device alarm of that site that slipped out first.
* If only one device at a site dies, it still raises its own "Device unreachable".
* Parents follow the main link, never a backup radio. `sitedown` refuses a site whose main-link radio is silent
  while the site's devices answer (that radio cannot be the live link) and tells you so.
* Sites listed in `DISASTER_SITES` (default: the core site) raise their Site DOWN as Disaster, the rest as High.

## Install

```
source ~/.zabbix_env
python3 tools/zbx_sites.py templates --apply          # adds template "MDP Site Status"
python3 tools/zbx_sites.py sitedown                   # dry run: read what it will change
python3 tools/zbx_sites.py sitedown --apply
python3 tools/topo_check.py                           # should list no wrong parents
```

After adding sites, or when a main link changes, edit `data/main_links.txt` and run `sitedown --apply` again.

## Undo

In Zabbix delete the hosts "Site SITExxxx", the template "MDP Site Status" and, under Data collection >
Event correlation, the two "MDP: Site down ..." rules.
