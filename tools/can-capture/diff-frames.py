#!/usr/bin/env python3
"""Surface the roof command candidates by diffing a BASELINE capture against an
OPERATE (switch-held) capture, and time-aligning to the 0x472 roof-status clock.

Reports:
  1. Roof-segment IDs present ONLY while operating (absent at idle) -- prime
     candidates for the switch/command activity.
  2. Roof-segment IDs whose DATA changed vs baseline.
  3. (with --dash) the roof-segment frames in the LEAD window just before each
     0x472 RoofGraphicStatus transition -- the trigger LEADS the dash animation.

Remember the corrected model (see mx5-roof-window-can): the roof is moved by
SWITCH EMULATION + suppressing the speed/reverse INTERLOCK frame, not by injecting
a magic command. So also watch for the interlock frame whose value changes when
you roll the car / select reverse in a separate listen-only run.

Logs are 'candump -ta' format:   (<ts>)  <iface>  <ID>  [len]  <bytes...>
Usage:
  ./diff-frames.py baseline-can1.log roof-open-can1.log --dash roof-open-can0.log
"""

import argparse
import re

_LINE = re.compile(r"\(([\d.]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)\s+\[\d+\]\s+(.*)")


def parse(path):
    out = []
    with open(path) as fh:
        for ln in fh:
            m = _LINE.search(ln)
            if m:
                ts, iface, cid, data = m.groups()
                out.append((float(ts), iface, cid.upper(), data.strip().upper()))
    return out


def _payloads(frames):
    d = {}
    for _, _, cid, data in frames:
        d.setdefault(cid, set()).add(data)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline", help="roof-segment idle capture")
    ap.add_argument("operate", help="roof-segment switch-held capture")
    ap.add_argument("--dash", help="dash-bus capture (same session) for 0x472 timing")
    ap.add_argument("--status-id", default="472")
    ap.add_argument("--lead-ms", type=float, default=250.0)
    a = ap.parse_args()

    base, op = parse(a.baseline), parse(a.operate)
    base_ids = {f[2] for f in base}
    bd, od = _payloads(base), _payloads(op)

    print("== 1. roof-segment IDs present ONLY while operating ==")
    only = sorted(set(od) - base_ids)
    for cid in only:
        print("   %s   payloads: %s" % (cid, " ".join(sorted(od[cid])[:4])))
    if not only:
        print("   (none isolated -- the trigger ID also appears at idle; see #2/#3)")

    print("\n== 2. roof-segment IDs whose DATA changed vs baseline ==")
    any_changed = False
    for cid in sorted(set(od) & base_ids):
        new = od[cid] - bd[cid]
        if new:
            any_changed = True
            print("   %s   new payloads: %s" % (cid, " ".join(sorted(new)[:4])))
    if not any_changed:
        print("   (no payload changes)")

    if a.dash:
        sid = a.status_id.upper()
        dash = parse(a.dash)
        status = [(ts, data) for ts, _, cid, data in dash if cid == sid]
        trans = [status[i] for i in range(1, len(status))
                 if status[i][1] != status[i - 1][1]]
        win = a.lead_ms / 1000.0
        print("\n== 3. roof-segment frames leading each %s change (<%dms) ==" % (sid, a.lead_ms))
        if not status:
            print("   (no %s frames in the dash log -- check --status-id / the bus)" % sid)
        for ts, data in trans:
            lead = sorted({cid for t, _, cid, _ in op if ts - win <= t < ts})
            print("   %s -> %s   led by: %s" % (sid, data, " ".join(lead) or "(nothing)"))


if __name__ == "__main__":
    main()
