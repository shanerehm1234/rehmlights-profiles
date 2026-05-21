# VIBE Online Profile Repository

## Goals
- Free the firmware binary of the 1.4 MB factory profile bundle (currently 54% of binary).
- Update fixture profiles without re-flashing firmware.
- Let the community contribute / fix profiles via PRs.
- Bridge to GDTF (the industry standard for fixture definitions).

## Architecture

```
┌──────────────────────────────────────────┐
│ rehmlights/vibe-profiles (GitHub repo)   │
│                                          │
│  /sources/<mfg>/<model>.json             │  ← human-edited source
│  /profiles/<id>.vp                       │  ← cooked binary (CI builds)
│  /index.json                             │  ← CI builds the index
│                                          │
│  CI on PR merge → cooks .vp + index.json │
│  GitHub Pages serves /profiles + /index  │
└──────────────────────────────────────────┘
                    │
                    │  HTTPS GET (mDNS-style discovery later)
                    ▼
┌──────────────────────────────────────────┐
│ VIBE P4 device                           │
│                                          │
│  SETUP wizard "+ Add fixture" → search   │
│  index.json → download .vp → store in    │
│  NVS (or SPIFFS partition for many)      │
└──────────────────────────────────────────┘
```

## Hosting

Recommended: **GitHub Pages**. Free, fast (Cloudflare CDN), built-in CI.

Domain: pick one (rehmlights.com/vibe-profiles, vibe-profiles.io, etc.) and CNAME it.

Until a custom domain exists, the device can hit `https://rehmlights.github.io/vibe-profiles/` directly. CORS is wide-open by default on Pages.

## Wire format

Two formats — JSON source of truth in the repo, cooked binary `.vp` served to devices.

### JSON source (`/sources/<mfg>/<model>.json`)

Human-editable. The CI cooker writes the binary form. Schema mirrors `vibe_profile_t` from `components/vibe_profile/include/vibe_profile.h`.

```json
{
  "schema":     "vibe-profile/1",
  "id":         "shehds-100w-moving-head-gobo-14ch",
  "name":       "100W Moving Head Gobo",
  "manufacturer": "SHEHDS",
  "mode":       "14ch",
  "footprint":  14,

  "channels": [
    { "role": "DIMMER",     "offset": 1,  "default": 0   },
    { "role": "SHUTTER",    "offset": 2,  "default": 8   },
    { "role": "PAN",        "offset": 3,  "default": 128 },
    { "role": "TILT",       "offset": 4,  "default": 128 },
    { "role": "PAN_TILT_SPEED", "offset": 5, "default": 0 },
    { "role": "COLOR_WHEEL","offset": 6 },
    { "role": "GOBO_WHEEL", "offset": 7 },
    { "role": "GOBO_ROT",   "offset": 9 },
    { "role": "PRISM",      "offset": 11 },
    { "role": "PAN_FINE",   "offset": 12 },
    { "role": "TILT_FINE",  "offset": 13 },
    { "role": "RESET",      "offset": 14 }
  ],

  "color_wheel": [
    { "value": 0,  "name": "Open" },
    { "value": 5,  "name": "Red" },
    { "value": 10, "name": "Green" },
    { "value": 15, "name": "Blue" },
    { "value": 20, "name": "Yellow" },
    { "value": 25, "name": "Magenta" },
    { "value": 30, "name": "Cyan" },
    { "value": 35, "name": "Green 2" }
  ],

  "gobo_wheel": [
    { "value": 0,  "name": "Open" },
    { "value": 10, "name": "Diamonds" },
    { "value": 20, "name": "Sun" },
    { "value": 30, "name": "Small Grid" }
  ],

  "values": {
    "shutter_open":  8,
    "shutter_close": 0,
    "strobe_slow":   16,
    "strobe_fast":   240,
    "dimmer_min":    0,
    "dimmer_max":    255
  },

  "verified":    true,
  "source":      "manufacturer-manual",
  "tested_on":   "2026-05-18"
}
```

**Role names** (in JSON, must match the `vibe_channel_role_t` enum minus the `VIBE_CH_` prefix): `PAN, PAN_FINE, TILT, TILT_FINE, PAN_TILT_SPEED, DIMMER, DIMMER_FINE, SHUTTER, STROBE, RED, GREEN, BLUE, WHITE, AMBER, UV, LIME, CYAN, MAGENTA, YELLOW, CTO, CTB, COLOR_WHEEL, COLOR_WHEEL_2, GOBO_WHEEL, GOBO_WHEEL_2, GOBO_ROT, GOBO_ROT_2, GOBO_SHAKE, PRISM, PRISM_ROT, PRISM_2, PRISM_ROT_2, FROST, FROST_2, ZOOM, IRIS, FOCUS, ANIMATION, LAMP, RESET, FAN, MACRO, EFFECTS, EFFECTS_SPEED, CUSTOM_1, CUSTOM_2, CUSTOM_3, CUSTOM_4`.

### Index file (`/index.json`)

CI builds this on every merge to `main`. Lets the device do a single GET to find matching fixtures.

```json
{
  "schema":     "vibe-index/1",
  "generated":  "2026-05-18T12:00:00Z",
  "count":      8,
  "fixtures": [
    {
      "id":         "shehds-100w-moving-head-gobo-14ch",
      "name":       "100W Moving Head Gobo",
      "manufacturer": "SHEHDS",
      "mode":       "14ch",
      "footprint":  14,
      "url":        "/profiles/shehds-100w-moving-head-gobo-14ch.vp",
      "sha256":     "abc123...",
      "verified":   true
    }
    /* ... */
  ]
}
```

### Cooked binary (`/profiles/<id>.vp`)

Packed serialization of `vibe_profile_t`. Exact layout TBD when the cooker is written — for v1 we can just dump the struct (little-endian) with a small header:

```
offset  size   field
0       4      magic    = 'V', 'P', 0x01, 0x00   (VP version 1)
4       4      length   (uint32_le)
8       N      packed vibe_profile_t body
```

Why binary vs JSON on the wire: parses ~10× faster on the device (no cJSON allocations), and the device cooker code is identical to what the legacy converter already does — we already cook to `vibe_profile_t` in RAM.

## Device fetch flow

1. User taps SETUP → "+ Add fixture (online)"
2. Device GET `https://<host>/index.json`
3. UI shows searchable list (manufacturer + name + mode)
4. User picks one → device GET `https://<host>/profiles/<id>.vp`
5. Verify SHA256 against index entry
6. Save to NVS under key `"prf_<id>"` (max 32 fixtures cached)
7. Add to engine universe at user-specified start channel

## Sample starter set

Ship these 20 cooked profiles bundled in firmware as fallback for offline first-boot:

- Generic 3ch RGB, 4ch RGBW, 5ch RGBWA, 6ch RGBWAUV LED pars
- Generic 7ch + 16ch moving wash
- SHEHDS 100W Moving Head Gobo (this is exactly what we have today, low risk)
- Chauvet SlimPAR Q12 USB
- ADJ Mega Bar TRIPAR
- Custom blank template

## Repo layout (for when you stand it up)

```
rehmlights/vibe-profiles/
├── README.md          # how to contribute
├── sources/           # human-edited JSON
│   ├── shehds/100w-moving-head-gobo-14ch.json
│   ├── chauvet/slimpar-q12-usb.json
│   └── ...
├── profiles/          # cooked .vp (generated by CI, gitignored or committed)
├── index.json         # generated by CI
├── tools/
│   └── cook.py        # JSON → .vp
└── .github/workflows/
    └── publish.yml    # cook + commit + GH Pages deploy
```

`cook.py` outline:
```python
import json, struct, sys, hashlib, glob

# enum order must match vibe_channel_role_t in vibe_profile.h
ROLES = ["NONE","PAN","PAN_FINE","TILT","TILT_FINE","PAN_TILT_SPEED",
         "DIMMER","DIMMER_FINE","SHUTTER","STROBE",
         "RED","GREEN","BLUE","WHITE","AMBER","UV","LIME",
         "CYAN","MAGENTA","YELLOW","CTO","CTB",
         "COLOR_WHEEL","COLOR_WHEEL_2",
         "GOBO_WHEEL","GOBO_WHEEL_2","GOBO_ROT","GOBO_ROT_2","GOBO_SHAKE",
         "PRISM","PRISM_ROT","PRISM_2","PRISM_ROT_2",
         "FROST","FROST_2","ZOOM","IRIS","FOCUS","ANIMATION",
         "LAMP","RESET","FAN","MACRO","EFFECTS","EFFECTS_SPEED",
         "CUSTOM_1","CUSTOM_2","CUSTOM_3","CUSTOM_4"]

def cook(src):
    body = bytearray()
    # ... pack vibe_profile_t fields in struct order
    # see components/vibe_profile/include/vibe_profile.h
    out = b'VP\x01\x00' + struct.pack('<I', len(body)) + bytes(body)
    return out

for path in glob.glob('sources/**/*.json', recursive=True):
    j = json.load(open(path))
    with open(f"profiles/{j['id']}.vp", 'wb') as f:
        f.write(cook(j))

# build index
idx = {"schema": "vibe-index/1", "fixtures": []}
for path in glob.glob('profiles/*.vp'):
    pid = path.split('/')[-1][:-3]
    src = json.load(open(f"sources/**/{pid}.json"))   # find source
    sha = hashlib.sha256(open(path,'rb').read()).hexdigest()
    idx['fixtures'].append({...})
json.dump(idx, open('index.json','w'), indent=2)
```

## Migration plan

Phase 1 (now): commit this doc + sample JSON profiles to the device repo as reference. Build the device-side fetch client.

Phase 2: stand up `rehmlights/vibe-profiles` GitHub repo + CI. Convert a curated ~20 starter profiles from the existing factory_profiles.c to JSON. Auto-cook.

Phase 3: device "+ Add fixture (online)" UI in the setup wizard.

Phase 4: bulk-convert remaining 390 v1.2 profiles to JSON (one-shot script). Community PRs flow in from here.

Phase 5: deprecate the in-firmware factory_profiles.c blob, replace with ~20-profile starter set burnt in via `xxd -i`. Saves ~1.3 MB binary.
