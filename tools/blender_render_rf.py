"""Render the top-down RF car-asset set for rubyhud's body/safety overlay.

Outputs the 8 door/trunk-combination PNGs + adjacent.png described in
hud/rubyhud/assets/car/README.md (top-down, nose up, transparent, aligned).

This is a Mac/desktop step, NOT run on the Pi. It needs Blender (>= 3.x) and a
downloaded ND MX-5 RF model. Recommended source (free, CC-BY — credit required):
  https://sketchfab.com/3d-models/mazda-mx-5-rf-dd61449aba724d93a5b4d6e0eaf2bd06

Run headless:
  blender --background --python tools/blender_render_rf.py

Most models are static meshes whose doors/trunk are separate objects but NOT
rigged. We "open" a panel by parenting it to an empty placed at the hinge and
rotating that empty. The three CONFIG blocks below (MODEL/OUT, DOOR/TRUNK object
names + hinge pivots + angles) are the ONLY things that must be tuned per model:
open the model in Blender once, click each door/trunk, read its object name and
hinge location from the N-panel, and fill them in.
"""

import math
import os

import bpy
import mathutils

# --------------------------------------------------------------------------- #
# CONFIG — EDIT THESE FOR YOUR MODEL
# --------------------------------------------------------------------------- #
MODEL = os.path.expanduser("~/Downloads/mazda_mx5_rf.glb")   # EDIT: model path
OUT = os.path.expanduser(                                    # EDIT: repo path
    "~/Documents/Claude/Projects/Ruby upgrade/code/ruby-hud/hud/rubyhud/assets/car")
RES = (900, 1200)            # output PNG size (w, h)
NOSE = "+Y"                  # which world axis the car nose points along

# Per-panel rig: object name (substring match), hinge pivot (world XYZ in metres,
# read off the model), rotation axis, and open angle (degrees). EDIT all of this
# after inspecting the model — these placeholders WILL be wrong for your mesh.
PANELS = {
    "L": dict(name="door_l", pivot=(0.78, 0.30, 0.70), axis="Z", angle=-62),
    "R": dict(name="door_r", pivot=(-0.78, 0.30, 0.70), axis="Z", angle=62),
    "T": dict(name="trunk",  pivot=(0.0, -1.85, 0.78), axis="X", angle=-58),
}
ADJACENT_DIM = 0.62          # brightness multiplier for adjacent.png
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
    name = name_substr.lower()
    hits = [o for o in bpy.data.objects
            if o.type == "MESH" and name in o.name.lower()]
    if not hits:
        print("WARNING: no mesh matches %r — panel will not move. "
              "Fix PANELS[...]['name']." % name_substr)
    return hits


def _rig_panel(cfg):
    """Parent matching panel meshes to an empty at the hinge; return the empty."""
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


def _setup_camera_and_light():
    scene = bpy.context.scene
    # Orthographic camera straight down (top view).
    cam_data = bpy.data.cameras.new("topcam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("topcam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (0.0, 0.0, 30.0)
    cam.rotation_euler = (0.0, 0.0, 0.0)
    scene.camera = cam
    # Fit ortho scale to the car bounds (with margin) after import.
    _fit_ortho(cam_data)

    # Soft studio light: a sun + two area fills (cool key, no harsh specular).
    sun = bpy.data.lights.new("sun", "SUN"); sun.energy = 2.2
    so = bpy.data.objects.new("sun", sun); so.rotation_euler = (math.radians(35), 0, math.radians(20))
    scene.collection.objects.link(so)
    for x in (-6, 6):
        a = bpy.data.lights.new("fill", "AREA"); a.energy = 350; a.size = 8
        ao = bpy.data.objects.new("fill", a); ao.location = (x, 0, 10)
        scene.collection.objects.link(ao)

    # Transparent film + clean color.
    scene.render.film_transparent = True
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 128
    scene.render.resolution_x, scene.render.resolution_y = RES
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"


def _fit_ortho(cam_data):
    xs, ys = [], []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            wc = o.matrix_world @ mathutils.Vector(c)
            xs.append(wc.x); ys.append(wc.y)
    if xs and ys:
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        cam_data.ortho_scale = span * 1.35   # margin for swung doors


def _render(path):
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("rendered", path)


def main():
    _clear_scene()
    _import(MODEL)
    _setup_camera_and_light()
    empties = {k: _rig_panel(cfg) for k, cfg in PANELS.items()}

    os.makedirs(OUT, exist_ok=True)
    for L in (0, 1):
        for R in (0, 1):
            for T in (0, 1):
                _set_open(empties["L"], PANELS["L"], L)
                _set_open(empties["R"], PANELS["R"], R)
                _set_open(empties["T"], PANELS["T"], T)
                _render(os.path.join(OUT, "car_%d%d%d.png" % (L, R, T)))

    # adjacent (blind-spot) car: closed, dimmer.
    for k in empties:
        _set_open(empties[k], PANELS[k], 0)
    bpy.context.scene.view_settings.exposure = math.log2(ADJACENT_DIM)
    _render(os.path.join(OUT, "adjacent.png"))
    print("DONE — 8 states + adjacent.png in", OUT)


if __name__ == "__main__":
    main()
