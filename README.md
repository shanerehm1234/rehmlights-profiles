# Rehmlights Profiles

Shared fixture-profile catalog for **VIBE** and **VECTR** DMX lighting controllers.

Devices fetch fixture personalities (channel maps, color/gobo wheels, strobe values, etc.) from this repo over HTTPS. New fixtures land here as JSON; the firmware fetches and uses them without a re-flash.

## What's here

```
sources/<manufacturer>/<model-mode>.json   ← one file per (fixture × DMX mode)
index.json                                  ← lightweight catalog the device hits first
tools/
  gdtf_to_json.py                           ← convert a .gdtf file into our JSON
  build_index.py                            ← regenerate index.json from sources/
schema.json                                 ← JSON Schema for source validation
docs/
  architecture.md                           ← wire format + device fetch flow
```

## How a device uses this

1. User taps "Add fixture from library" on the device or web UI.
2. Device fetches `https://raw.githubusercontent.com/shanerehm1234/rehmlights-profiles/main/index.json` — a few KB listing every fixture.
3. UI shows a scrollable, searchable list. User picks one.
4. Device fetches `sources/<id>.json`, cooks it through the existing `vibe_profile_from_legacy()` step, saves to NVS.
5. Profile is now available offline forever; "check for updates" re-fetches the index.

## Contributing a new profile

You have two paths:

### Path A — fixture exists on [GDTF Share](https://gdtf-share.com/)

Most modern fixtures do. GDTF is the industry-standard format from MA Lighting, Robe, and Vectorworks.

```bash
# Download the .gdtf file from gdtf-share.com (free account required)
python tools/gdtf_to_json.py path/to/fixture.gdtf
# Output: sources/<manufacturer>/<model-mode>.json (one per DMX mode)
python tools/build_index.py
git add sources/ index.json && git commit -m "Add <fixture name>" && git push
```

Open a PR. CI validates the JSON against the schema. After merge, devices see the new profile within minutes.

### Path B — manual authoring

Use the existing custom profile editor on the device's web UI. Export the JSON it writes. Drop it into `sources/<manufacturer>/<id>.json`. Submit a PR.

### Path C — fixture not in GDTF, not on a device

Hand-edit a JSON file matching `schema.json`. See the existing files in `sources/` for reference. Channel offsets are 1-based DMX positions within the fixture's footprint.

## Schema versioning

`schema.json` is `vibe-profile/legacy-v1.2`. The schema is locked at this version — additions are non-breaking (new optional fields), removals are forbidden. If a wholesale rework is needed, it's `legacy-v2` and the device fetcher decides which schema it speaks.

## Why JSON over the wire (not binary `.vp`)

The original design doc proposed a packed-struct binary format for ~10× faster device parsing. We deferred that to v2 — at a few KB per profile, JSON parse latency is invisible on this hardware. Switch to binary the moment we have evidence it matters.

## License

Profile data here is contributed under CC0 — public domain. The conversion tools (`tools/`) are MIT.

DMX channel mappings are factual technical data and not copyrightable in most jurisdictions. Manufacturer names and gobo names are trademarks of their respective owners; included here strictly for fixture identification.
