"""Render the top-down RF TURNTABLE for rubyhud's rotating body view.

Outputs a 360-degree orbit of the ND MX-5 RF at a fixed 3/4 elevation, for each
door/trunk state, as transparent PNGs:

    assets/car/tt/<L><R><T>_<deg>.png      e.g. tt/100_045.png

where <L><R><T> = DoorLeft DoorRight Trunk (1 = open) and <deg> is the orbit
azimuth (000, 015, 030 ... for ANGLES=24). The on-device viewer (carview.py)
blits the frame nearest the live angle + state; it does NOT do 3D at runtime.

This is a Mac/desktop step (NOT the Pi). Needs Blender (>= 3.x) and a downloaded
ND MX-5 RF model. Free CC-BY source (credit required):
  https://sketchfab.com/3d-models/mazda-mx-5-rf-dd61449aba724d93a5b4d6e0eaf2bd06

Run headless:
  blender --background --python tools/blender_render_rf.py

The CONFIG block + PANELS (door/trunk object names, hinge pivots, angles) are the
only per-model tuning: open the model once in Blender, click each door/trunk,
read its object name + hinge location off the N-panel, and fill them in. The
placeholders below WILL be wrong for your mesh.

Frame budget: ANGLES * len(COMBOS) renders. Default 24 * 8 = 192 PNGs (a few
minutes, one-time). Drop COMBOS to {"000","100","010","001"} for a lean 96.
"""

import math
import os

import bpy
import mathutils

# --------------------------------------------------------------------------- #
# CONFIG — EDIT FOR YOUR MODEL
# --------------------------------------------------------------------------- #
MODEL = os.path.expanduser("~/Downloads/mazda_mx5_rf.glb")        # EDIT
OUT = os.path.expanduser(                                          # EDIT
    "~/Documents/Claude/Projects/Ruby upgrade/code/ruby-hud/hud/rubyhud/assets/car/tt")
RES = (1000, 1000)        # square so the car never clips as it orbits
ANGLES = 24               # orbit steps (24 -> every 15 deg). 36 = silkier.
ELEV = 32.0               # camera elevation above horizontal (deg); 90 = top-down
DIST = 6.0                # camera distance (world units; tune to model scale)
ORTHO = True              # orthographic keeps scale constant through the spin

# Which door/trunk states to render. Full set = every combination; trim to save
# frames if multi-open combos don't matter to you.
COMBOS = ["000", "100", "010", "001", "110", "101", "011", "111"]

# Per-panel rig: object name (substring), hinge pivot (world XYZ, read off the
# model), rotation axis, open angle (deg). EDIT after inspecting the model.
PANELS = {
    "L": dict(name="door_l", pivot=(0.78, 0.30, 0.70), axis="Z", angle=-62),
    "R": dict(name="door_r", pivot=(-0.78, 0.30, 0.70), axis="Z", angle=62),
    "T": dict(name="trunk",  pivot=(0.0, -1.85, 0.78), axis="X", angle=-58),
}
# --------------------------------------------------------------------------- #


def _clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def _import(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise SystemExit("Unsupported model format: %s" % ext)


def _find(name_substr):
    n = name_substr.lower()
    hits = [o for o in bpy.data.objects
            if o.type == "MESH" and n in o.name.lower()]
    if not hits:
        print("WARNING: no mesh matches %r — panel won't move. Fix PANELS." % name_substr)
    return hits


def _rig_panel(cfg):
    empty = bpy.data.objects.new("hinge_%s" % cfg["name"], None)
    bpy.context.scene.collection.objects.link(empty)
    empty.location = mathutils.Vector(cfg["pivot"])
    for o in _find(cfg["name"]):
        o.parent = empty
        o.matrix_parent_inverse = empty.matrix_world.inverted()
    return empty


def _set_open(empty, cfg, is_open):
    ang = math.radians(cfg["angle"]) if is_open else 0.0
    rot = [0.0, 0.0, 0.0]
    rot["XYZ".index(cfg["axis"])] = ang
    empty.rotation_euler = rot


def _model_center_radius():
    xs, ys, zs = [], [], []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            wc = o.matrix_world @ mathutils.Vector(c)
            xs.append(wc.x); ys.append(wc.y); zs.append(wc.z)
    if not xs:
        return mathutils.Vector((0, 0, 0)), 2.0
    ctr = mathutils.Vector(((min(xs) + max(xs)) / 2,
                            (min(ys) + max(ys)) / 2,
                            (min(zs) + max(zs)) / 2))
    rad = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) / 2
    return ctr, rad


def _setup_turntable_rig():
    scene = bpy.context.scene
    ctr, rad = _model_center_radius()

    # Pivot empty at the car centre; the camera is parented and the pivot spins.
    pivot = bpy.data.objects.new("orbit_pivot", None)
    pivot.location = ctr
    scene.collection.objects.link(pivot)

    cam_data = bpy.data.cameras.new("orbcam")
    cam = bpy.data.objects.new("orbcam", cam_data)
    scene.collection.objects.link(cam)
    el = math.radians(ELEV)
    cam.location = (0.0, -DIST * math.cos(el), DIST * math.sin(el))
    cam.rotation_euler = (math.radians(90.0) - el, 0.0, 0.0)   # look at pivot
    cam.parent = pivot
    cam.matrix_parent_inverse = pivot.matrix_world.inverted()
    scene.camera = cam
    if ORTHO:
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = rad * 2.4

    # Soft studio light (parented so highlights stay put as the car spins).
    sun = bpy.data.lights.new("sun", "SUN"); sun.energy = 2.4
    so = bpy.data.objects.new("sun", sun)
    so.rotation_euler = (math.radians(40), 0, math.radians(25))
    scene.collection.objects.link(so)
    for x in (-rad * 3, rad * 3):
        a = bpy.data.lights.new("fill", "AREA"); a.energy = 400; a.size = rad * 6
        ao = bpy.data.objects.new("fill", a); ao.location = (x, 0, rad * 4)
        scene.collection.objects.link(ao)

    scene.render.film_transparent = True
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 128
    scene.render.resolution_x, scene.render.resolution_y = RES
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    return pivot


def _render(path):
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main():
    _clear_scene()
    _import(MODEL)
    empties = {k: _rig_panel(cfg) for k, cfg in PANELS.items()}
    pivot = _setup_turntable_rig()
    os.makedirs(OUT, exist_ok=True)

    step = 360.0 / ANGLES
    for combo in COMBOS:
        L, R, T = (c == "1" for c in combo)
        _set_open(empties["L"], PANELS["L"], L)
        _set_open(empties["R"], PANELS["R"], R)
        _set_open(empties["T"], PANELS["T"], T)
        for i in range(ANGLES):
            deg = int(round(i * step))
            pivot.rotation_euler = (0.0, 0.0, math.radians(i * step))
            _render(os.path.join(OUT, "%s_%03d.png" % (combo, deg)))
            print("rendered %s_%03d" % (combo, deg))

    print("DONE — %d frames in %s" % (ANGLES * len(COMBOS), OUT))


if __name__ == "__main__":
    main()
