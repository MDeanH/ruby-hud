# Top-down RF car assets (body / blind-spot overlay)

`body_overlay.py` composites these PNGs for the photoreal Tesla-style vehicle
visualization. **They are optional** — if absent, the overlay draws a vector
fallback car, so the HUD always works. Drop the files here to upgrade the look.

## Files

| File | Contents |
|---|---|
| `car_000.png` | ego car, everything closed (also the fallback base) |
| `car_100.png` | driver (left) door open |
| `car_010.png` | passenger (right) door open |
| `car_001.png` | trunk open |
| `car_110.png` | both doors open |
| `car_101.png` | driver door + trunk open |
| `car_011.png` | passenger door + trunk open |
| `car_111.png` | both doors + trunk open |
| `adjacent.png` | the blind-spot vehicle (top-down, dimmer); auto-mirrored for the left side |

The filename digits are `car_<L><R><T>.png` = DoorLeft, DoorRight, Trunk
(`1` = open). The overlay picks the file matching the live state in one paste.

## Framing contract (so all 8 align)

- **Top-down, orthographic, nose pointing UP.** Same camera for every state.
- **Transparent background** (RGBA, straight alpha).
- Car centered: the *body centroid* sits at the canvas center. Open doors/trunk
  protrude beyond the body but the centroid must not move between states, or the
  car will appear to jump when a door opens.
- Suggested canvas ~900 x 1200 px (portrait). The overlay scales by height
  (car ≈ 506 px tall on screen at SS=2 → ~1012 px) and centers, so exact size
  is flexible; consistency across states is what matters.
- Soft contact shadow is added by the overlay — render the car alone (no baked
  ground shadow needed, though a subtle one is fine).

`adjacent.png` is a single top-down car (doors closed), same framing; the
overlay scales and horizontally flips it for whichever side is occupied.

## Attribution

The reference model is **CC-BY** and must be credited where the project lists
third-party assets (README / CHANGELOG), e.g.:

> Car render based on "Mazda MX-5 RF" by MimmoLagonigro (Sketchfab), CC BY 4.0.

Generate the PNGs with `tools/blender_render_rf.py` (see that file's header).
