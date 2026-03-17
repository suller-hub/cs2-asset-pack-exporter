bl_info = {
    "name": "CS2 Asset Pack Exporter",
    "author": "CS2 Modding Tool",
    "version": (2, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > CS2 Export",
    "description": "Export Blender collections as CS2-ready asset packs",
    "category": "Import-Export",
}

import bpy
import os
import re
import json
import subprocess
import sys
import tempfile
import numpy as np
from bpy.props import (
    StringProperty, BoolProperty, EnumProperty,
    IntProperty, FloatProperty, PointerProperty,
    CollectionProperty,
)
from bpy.types import Panel, Operator, PropertyGroup, AddonPreferences


# ===========================================================================
# ADDON PREFERENCES  (default export folder stored here, not per-scene)
# ===========================================================================

class CS2ExporterPreferences(AddonPreferences):
    bl_idname = __name__

    default_export_folder: StringProperty(
        name="Default Export Folder",
        description="Default root folder for all CS2 asset pack exports",
        default="",
        subtype="DIR_PATH",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "default_export_folder")
        layout.label(text="This folder is used when no pack-specific folder is set.",
                     icon="INFO")


# ===========================================================================
# PER-COLLECTION IGNORE LIST
# ===========================================================================

def _on_export_mode_update(self, context):
    """Auto-create age stage sub-collections when Aging Tree mode is selected."""
    if self.export_mode != "AGING_TREE":
        return
    col = bpy.data.collections.get(self.name)
    if not col:
        return
    for stage in AGING_TREE_STAGES:
        if stage not in [c.name for c in col.children]:
            new_col = bpy.data.collections.new(stage)
            col.children.link(new_col)


class CS2CollectionIgnore(PropertyGroup):
    name:   StringProperty()
    ignore: BoolProperty(name="Ignore", default=False)
    export_mode: bpy.props.EnumProperty(
        name="Export Mode",
        description="How to export meshes in this collection",
        items=[
            ("VARIANTS",       "Variants",          "Each mesh in its own subfolder (for CS2 mesh variations)"),
            ("SINGLE_MESH",    "Single Mesh",        "Merge all meshes into one FBX (single material)"),
            ("SPLIT_MATERIAL", "Split per Material", "One FBX per material slot, shared folder"),
            ("AGING_TREE",     "Aging Tree",         "Child collections mapped to CS2 tree age stages (Child/Teen/Adult/Elderly/Dead/Stump)"),
        ],
        default="VARIANTS",
        update=_on_export_mode_update,
    )


def _sync_ignore_list(context):
    ignore_list = context.scene.cs2_collection_ignores
    scene_cols  = {c.name for c in context.scene.collection.children
                   if any(o.type == "MESH" for o in c.objects)}
    existing    = {e.name for e in ignore_list}

    for name in scene_cols:
        if name not in existing:
            e      = ignore_list.add()
            e.name = name

    stale = [e.name for e in ignore_list if e.name not in scene_cols]
    for name in stale:
        idx = next((i for i, e in enumerate(ignore_list) if e.name == name), None)
        if idx is not None:
            ignore_list.remove(idx)


def _is_ignored(context, col_name):
    for e in context.scene.cs2_collection_ignores:
        if e.name == col_name:
            return e.ignore
    return False


# ===========================================================================
# SCENE SETTINGS  (pack name + optional folder override)
# ===========================================================================

class CS2PackSettings(PropertyGroup):

    pack_name: StringProperty(
        name="Pack Name",
        description="Name of the asset pack (output folder name)",
        default="MyAssetPack",
    )
    export_folder_override: StringProperty(
        name="Export Folder (override)",
        description="Leave empty to use the default folder from addon preferences",
        default="",
        subtype="DIR_PATH",
    )
    texture_size: EnumProperty(
        name="Texture Size",
        items=[
            ("512",  "512 px",  ""),
            ("1024", "1024 px", ""),
            ("2048", "2048 px", "Recommended"),
            ("4096", "4096 px", "Max CS2"),
        ],
        default="2048",
    )
    ao_samples: IntProperty(
        name="AO Samples",
        description="Cycles samples for AO bake",
        default=32, min=4, max=512, step=4,
    )
    export_fbx: BoolProperty(name="Export FBX",      default=True)
    export_textures: BoolProperty(name="Export Textures", default=True)
    do_decimate: BoolProperty(name="Auto Decimate",   default=False)
    polys_per_m3: FloatProperty(
        name="Polys / m³", default=2000.0, min=10.0, max=500000.0, step=100,
    )
    max_tris: IntProperty(
        name="Max Triangles", default=10000, min=100, max=200000, step=500,
    )


def _resolve_export_folder(context):
    """Return the active export folder: override → preferences → ''."""
    s = context.scene.cs2_pack_settings
    if s.export_folder_override.strip():
        return bpy.path.abspath(s.export_folder_override)
    prefs = context.preferences.addons[__name__].preferences
    if prefs.default_export_folder.strip():
        return bpy.path.abspath(prefs.default_export_folder)
    return ""


# ===========================================================================
# NAME SANITIZER
# ===========================================================================

def _sanitize(name):
    """Remove underscores so CS2 name parser doesn't choke."""
    base = re.sub(r'_?LOD\d+$', '', name, flags=re.IGNORECASE)
    return base.replace('_', '')


# ===========================================================================
# AGING TREE VALIDATION
# ===========================================================================

AGING_TREE_STAGES = {"Child", "Teen", "Adult", "Elderly", "Dead", "Stump"}


def _base_stage_name(name):
    """Strip Blender duplicate suffix: 'Child.001' -> 'Child'"""
    import re as _re
    return _re.sub(r'\.\d+$', '', name)


def _validate_aging_tree(collection):
    """
    Check that a collection set up for Aging Tree export has valid child collections.
    Returns (valid, errors) where errors is a list of strings.
    """
    errors = []
    child_cols = list(collection.children)

    if not child_cols:
        errors.append(f"'{collection.name}' has no child collections.")
        return False, errors

    for col in child_cols:
        base = _base_stage_name(col.name)
        if base not in AGING_TREE_STAGES:
            errors.append(
                f"Invalid stage name '{col.name}'. "
                f"Must be one of: {', '.join(sorted(AGING_TREE_STAGES))}"
            )

    return len(errors) == 0, errors


# ===========================================================================
# TEXTURE EXTRACTION
# ===========================================================================

def _get_principled(material):
    if not material or not material.use_nodes:
        return None
    for node in material.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    for node in material.node_tree.nodes:
        if node.type == "GROUP" and node.node_tree:
            for inner in node.node_tree.nodes:
                if inner.type == "BSDF_PRINCIPLED":
                    return inner
    return None


def _image_from_socket(socket):
    if not socket.is_linked:
        return None
    from_node = socket.links[0].from_node
    if from_node.type == "TEX_IMAGE":
        return from_node.image
    for inp in from_node.inputs:
        if inp.is_linked:
            deeper = inp.links[0].from_node
            if deeper.type == "TEX_IMAGE":
                return deeper.image
    return None


def _get_textures(material):
    result = {"base_color": None, "normal": None, "roughness": None, "metallic": None}
    if not material or not material.use_nodes:
        return result

    # Strategy 1: Principled BSDF sockets
    bsdf = _get_principled(material)
    if bsdf:
        for key, sock in [("base_color","Base Color"),("roughness","Roughness"),
                          ("metallic","Metallic"),("normal","Normal")]:
            s = bsdf.inputs.get(sock)
            if s:
                result[key] = _image_from_socket(s)

    # Strategy 2: frame label / filename keywords (Poly Haven etc.)
    kw_map = {
        "base_color": ["base color","basecolor","albedo","diffuse","diff","color","_bc","_d."],
        "normal":     ["normal","nrm","nor_gl","nor_dx","_nor","_n.","_nm"],
        "roughness":  ["roughness","rough","rgh","_r.","_ro"],
        "metallic":   ["metallic","metal","met","_m.","_mt"],
    }
    node_frame = {}
    for node in material.node_tree.nodes:
        if node.parent and node.parent.type == "FRAME":
            node_frame[node.name] = node.parent.label.lower()

    for node in material.node_tree.nodes:
        if node.type != "TEX_IMAGE" or not node.image:
            continue
        frame  = node_frame.get(node.name, "")
        img_nm = node.image.name.lower()
        for key, kws in kw_map.items():
            if result[key] is not None:
                continue
            for kw in kws:
                if kw in frame or kw in img_nm:
                    result[key] = node.image
                    break

    return result


def _first_material(collection):
    for obj in collection.objects:
        if obj.type == "MESH" and obj.data.materials:
            mat = obj.data.materials[0]
            if mat:
                return mat
    return None


# ===========================================================================
# TEXTURE SAVING
# ===========================================================================

def _px_to_np(image, w, h):
    img = image.copy()
    img.scale(w, h)
    arr = np.array(img.pixels[:], dtype=np.float32).reshape((h, w, 4))
    bpy.data.images.remove(img)
    return arr


def _save_png(pixels, filepath, w, h):
    img = bpy.data.images.new(os.path.basename(filepath), w, h, alpha=True)
    img.pixels = pixels.flatten().tolist()
    img.filepath_raw = filepath
    img.file_format  = "PNG"
    img.save()
    bpy.data.images.remove(img)


def _save_textures(textures, ao_image, asset_name, asset_dir, tex_size):
    w = h = tex_size
    saved = []

    if textures["base_color"]:
        fp = os.path.join(asset_dir, f"{asset_name}_BaseColor.png")
        _save_png(_px_to_np(textures["base_color"], w, h), fp, w, h)
        saved.append(fp)

    if textures["normal"]:
        fp = os.path.join(asset_dir, f"{asset_name}_Normal.png")
        _save_png(_px_to_np(textures["normal"], w, h), fp, w, h)
        saved.append(fp)

    mask = np.ones((h, w, 4), dtype=np.float32)
    mask[:,:,0] = _px_to_np(textures["metallic"],  w, h)[:,:,0] if textures["metallic"]  else 0.0
    mask[:,:,1] = _px_to_np(ao_image,              w, h)[:,:,0] if ao_image               else 1.0
    mask[:,:,2] = 1.0
    mask[:,:,3] = (1.0 - _px_to_np(textures["roughness"], w, h)[:,:,0]) if textures["roughness"] else 0.5

    fp = os.path.join(asset_dir, f"{asset_name}_MaskMap.png")
    _save_png(mask, fp, w, h)
    saved.append(fp)

    fp = os.path.join(asset_dir, f"{asset_name}_ControlMask.png")
    _save_png(np.ones((h, w, 4), dtype=np.float32), fp, w, h)
    saved.append(fp)

    return saved


# ===========================================================================
# AO BAKE
# ===========================================================================

def _bake_ao(mesh_objects, asset_name, tex_size, samples, report_fn):
    report_fn(f"INFO: Baking AO '{asset_name}' ({samples} samples)...")
    orig_engine  = bpy.context.scene.render.engine
    orig_samples = bpy.context.scene.cycles.samples
    bpy.context.scene.render.engine  = "CYCLES"
    bpy.context.scene.cycles.samples = samples

    ao_img = bpy.data.images.new(f"{asset_name}_AO", tex_size, tex_size, alpha=False)
    ao_img.colorspace_settings.name = "Non-Color"

    temp_nodes = []
    for obj in mesh_objects:
        for mat in (obj.data.materials or []):
            if not mat or not mat.use_nodes:
                continue
            node = mat.node_tree.nodes.new("ShaderNodeTexImage")
            node.image = ao_img
            node.name  = "__CS2_AO__"
            mat.node_tree.nodes.active = node
            temp_nodes.append((mat, node))

    bpy.ops.object.select_all(action="DESELECT")
    for obj in mesh_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_objects[0]

    try:
        bpy.ops.object.bake(type="AO", use_clear=True)
        success = True
    except Exception as e:
        report_fn(f"WARNING: AO bake failed: {e}")
        success = False

    for mat, node in temp_nodes:
        mat.node_tree.nodes.remove(node)

    bpy.context.scene.render.engine  = orig_engine
    bpy.context.scene.cycles.samples = orig_samples
    bpy.ops.object.select_all(action="DESELECT")

    if not success:
        bpy.data.images.remove(ao_img)
        return None
    return ao_img


# ===========================================================================
# DECIMATION
# ===========================================================================

def _volume_m3(obj):
    bb    = obj.bound_box
    scale = obj.matrix_world.to_scale()
    x = max(v[0] for v in bb) - min(v[0] for v in bb)
    y = max(v[1] for v in bb) - min(v[1] for v in bb)
    z = max(v[2] for v in bb) - min(v[2] for v in bb)
    return max(abs(x*scale.x) * abs(y*scale.y) * abs(z*scale.z), 0.001)


def _tri_count(obj):
    me = obj.to_mesh()
    me.calc_loop_triangles()
    n = len(me.loop_triangles)
    obj.to_mesh_clear()
    return n


def _decimate(obj, polys_per_m3, max_tris, report_fn):
    target  = min(int(polys_per_m3 * _volume_m3(obj)), max_tris)
    current = _tri_count(obj)
    if current <= target or target <= 0:
        return
    ratio = max(target / current, 0.01)
    report_fn(f"INFO:   Decimate '{obj.name}': {current}→~{target} tris")
    mod       = obj.modifiers.new("__CS2_DEC__", "DECIMATE")
    mod.ratio = ratio
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)


# ===========================================================================
# FBX EXPORT  (single object, non-destructive)
# ===========================================================================

def _export_fbx(obj, fbx_path):
    """Export one object. Scale is handled via global_scale=100."""
    orig_hide = obj.hide_viewport
    obj.hide_viewport = False

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.export_scene.fbx(
        filepath=fbx_path,
        use_selection=True,
        apply_unit_scale=False,
        apply_scale_options="FBX_SCALE_NONE",
        global_scale=1.0,
        bake_space_transform=True,
        object_types={"MESH"},
        use_mesh_modifiers=True,
        mesh_smooth_type="OFF",
        use_mesh_edges=False,
        path_mode="COPY",
        axis_forward="-Z",
        axis_up="Y",
    )

    obj.hide_viewport = orig_hide
    bpy.ops.object.select_all(action="DESELECT")


# ===========================================================================
# BACKGROUND EXPORT WORKER
# ===========================================================================

def _run_export_in_background(blend_path, export_data_path):
    """
    Launch a headless Blender instance to do the actual export.
    This keeps the main Blender UI responsive.
    """
    worker_script = os.path.join(tempfile.gettempdir(), "cs2_export_worker.py")

    script_content = f"""
import bpy, sys, os, json, re, numpy as np, tempfile

with open(r"{export_data_path}") as f:
    data = json.load(f)

exec(open(r"{os.path.abspath(__file__)}").read())

context_dummy = bpy.context

for item in data["items"]:
    col_name   = item["collection"]
    pack_dir   = item["pack_dir"]
    tex_size   = item["tex_size"]
    ao_samples = item["ao_samples"]
    do_tex     = item["do_textures"]
    do_dec     = item["do_decimate"]
    polys_m3   = item["polys_per_m3"]
    max_tris   = item["max_tris"]

    col = bpy.data.collections.get(col_name)
    if not col:
        print(f"WARNING: collection {{col_name}} not found")
        continue

    mesh_objects = [o for o in col.objects if o.type == "MESH"]
    asset_name   = _sanitize(col_name)
    col_dir      = os.path.join(pack_dir, asset_name)
    os.makedirs(col_dir, exist_ok=True)
    export_mode  = item.get("export_mode", "VARIANTS")

    if do_dec:
        for obj in mesh_objects:
            _decimate(obj, polys_m3, max_tris, print)

    # ── MODE: VARIANTS — each mesh in its own subfolder ───────────────────
    if export_mode == "VARIANTS":
        for idx, obj in enumerate(mesh_objects):
            var_name = asset_name if len(mesh_objects) == 1 else f"{{asset_name}}_{{chr(ord('a')+idx)}}"
            var_dir  = os.path.join(col_dir, var_name)
            os.makedirs(var_dir, exist_ok=True)
            _export_fbx(obj, os.path.join(var_dir, f"{{var_name}}.fbx"))
            if do_tex:
                mat = obj.data.materials[0] if obj.data.materials else None
                if mat:
                    ao = _bake_ao([obj], var_name, tex_size, ao_samples, print)
                    _save_textures(_get_textures(mat), ao, var_name, var_dir, tex_size)
                    if ao: bpy.data.images.remove(ao)

    # ── MODE: SINGLE MESH — join all meshes into one FBX ─────────────────
    elif export_mode == "SINGLE_MESH":
        bpy.ops.object.select_all(action="DESELECT")
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_objects[0]
        bpy.ops.object.duplicate()
        dupes = [o for o in bpy.context.selected_objects]
        bpy.ops.object.join()
        joined = bpy.context.active_object
        joined.name = asset_name
        var_dir = os.path.join(col_dir, asset_name)
        os.makedirs(var_dir, exist_ok=True)
        _export_fbx(joined, os.path.join(var_dir, f"{{asset_name}}.fbx"))
        if do_tex:
            mat = joined.data.materials[0] if joined.data.materials else None
            if mat:
                ao = _bake_ao([joined], asset_name, tex_size, ao_samples, print)
                _save_textures(_get_textures(mat), ao, asset_name, var_dir, tex_size)
                if ao: bpy.data.images.remove(ao)
        bpy.ops.object.delete()

    # ── MODE: SPLIT PER MATERIAL — one FBX per material slot ─────────────
    elif export_mode == "SPLIT_MATERIAL":
        # Collect all unique materials across all meshes
        mats_seen = {{}}
        for obj in mesh_objects:
            for mat in obj.data.materials:
                if mat and mat.name not in mats_seen:
                    mats_seen[mat.name] = mat

        for mat_name, mat in mats_seen.items():
            safe_mat = _sanitize(mat_name)
            fbx_name = f"{{asset_name}}_{{safe_mat}}"

            # Duplicate objects that use this material and separate by material
            bpy.ops.object.select_all(action="DESELECT")
            relevant = [o for o in mesh_objects if mat_name in [m.name for m in o.data.materials if m]]
            if not relevant:
                continue
            for obj in relevant:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = relevant[0]
            bpy.ops.object.duplicate()
            dupes = list(bpy.context.selected_objects)

            # Separate by material on each dupe
            for d in dupes:
                bpy.context.view_layer.objects.active = d
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="DESELECT")
                # Select faces with this material
                d.active_material_index = d.data.materials.find(mat_name)
                bpy.ops.object.material_slot_select()
                bpy.ops.mesh.select_all(action="INVERT")
                bpy.ops.mesh.delete(type="FACE")
                bpy.ops.object.mode_set(mode="OBJECT")

            # Join dupes into one
            bpy.ops.object.select_all(action="DESELECT")
            for d in dupes:
                d.select_set(True)
            bpy.context.view_layer.objects.active = dupes[0]
            if len(dupes) > 1:
                bpy.ops.object.join()
            joined = bpy.context.active_object
            joined.name = fbx_name

            # Export into col_dir (no subfolder — CS2 combines them)
            _export_fbx(joined, os.path.join(col_dir, f"{{fbx_name}}.fbx"))

            if do_tex:
                ao = _bake_ao([joined], fbx_name, tex_size, ao_samples, print)
                _save_textures(_get_textures(mat), ao, fbx_name, col_dir, tex_size)
                if ao: bpy.data.images.remove(ao)

            bpy.ops.object.delete()

    # ── MODE: AGING TREE — child collections as age stages ───────────────
    if export_mode == "AGING_TREE":
        aging_stages = {"Child", "Teen", "Adult", "Elderly", "Dead", "Stump"}
        def _base(n):
            import re as _re2
            return _re2.sub(r'\.\d+$', '', n)

        for child_col in col.children:
            if _base(child_col.name) not in aging_stages:
                print(f"WARNING: Skipping invalid stage '{{child_col.name}}'")
                continue
            stage_meshes = [o for o in child_col.objects if o.type == "MESH"]
            if not stage_meshes:
                continue

            # CS2 naming: assetName + "Tree" + stageName
            # e.g. quivertree02 + Tree + Adult = quivertree02TreeAdult
            stage_name = f"{{asset_name}}Tree{{_base(child_col.name)}}"
            stage_dir  = os.path.join(col_dir, stage_name)
            os.makedirs(stage_dir, exist_ok=True)

            if do_dec:
                for obj in stage_meshes:
                    _decimate(obj, polys_m3, max_tris, print)

            # Join stage meshes if multiple
            if len(stage_meshes) == 1:
                export_obj = stage_meshes[0]
                _export_fbx(export_obj, os.path.join(stage_dir, f"{{stage_name}}.fbx"))
                if do_tex:
                    mat = export_obj.data.materials[0] if export_obj.data.materials else None
                    if mat:
                        ao = _bake_ao([export_obj], stage_name, tex_size, ao_samples, print)
                        _save_textures(_get_textures(mat), ao, stage_name, stage_dir, tex_size)
                        if ao: bpy.data.images.remove(ao)
            else:
                bpy.ops.object.select_all(action="DESELECT")
                for obj in stage_meshes:
                    obj.select_set(True)
                bpy.context.view_layer.objects.active = stage_meshes[0]
                bpy.ops.object.duplicate()
                dupes = list(bpy.context.selected_objects)
                bpy.ops.object.join()
                joined = bpy.context.active_object
                joined.name = stage_name
                _export_fbx(joined, os.path.join(stage_dir, f"{{stage_name}}.fbx"))
                if do_tex:
                    mat = joined.data.materials[0] if joined.data.materials else None
                    if mat:
                        ao = _bake_ao([joined], stage_name, tex_size, ao_samples, print)
                        _save_textures(_get_textures(mat), ao, stage_name, stage_dir, tex_size)
                        if ao: bpy.data.images.remove(ao)
                bpy.ops.object.delete()

            print(f"  Stage done: {{stage_name}}")

    print(f"Done: {{col_name}} ({{export_mode}})")

print("EXPORT_COMPLETE")
"""
    with open(worker_script, 'w') as f:
        f.write(script_content)

    cmd = [
        bpy.app.binary_path,
        "--background",
        blend_path,
        "--python", worker_script,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


# ===========================================================================
# MAIN EXPORT OPERATOR
# ===========================================================================

class CS2_OT_ExportAssetPack(Operator):
    bl_idname      = "cs2.export_asset_pack"
    bl_label       = "Export Asset Pack"
    bl_description = "Export all collections as CS2-ready asset packs (background process)"

    _process  = None
    _timer    = None
    _log_path = None

    def modal(self, context, event):
        if event.type == "TIMER":
            if self._process.poll() is not None:
                output = self._process.stdout.read()
                if "EXPORT_COMPLETE" in output:
                    self.report({"INFO"}, "CS2 Export completed!")
                else:
                    self.report({"WARNING"}, "Export finished with warnings — check console.")
                context.window_manager.event_timer_remove(self._timer)
                context.workspace.status_text_set(None)
                return {"FINISHED"}
            context.workspace.status_text_set("CS2 Export running in background...")
        return {"PASS_THROUGH"}

    def execute(self, context):
        s             = context.scene.cs2_pack_settings
        export_folder = _resolve_export_folder(context)
        pack_name     = s.pack_name.strip() or "AssetPack"

        if not export_folder:
            self.report({"ERROR"}, "No export folder set. Configure in Addon Preferences.")
            return {"CANCELLED"}

        pack_dir = os.path.join(export_folder, pack_name)
        os.makedirs(pack_dir, exist_ok=True)

        _sync_ignore_list(context)

        collections = [
            col for col in context.scene.collection.children
            if any(o.type == "MESH" for o in col.objects)
            and not _is_ignored(context, col.name)
        ]

        if not collections:
            self.report({"WARNING"}, "No exportable collections found.")
            return {"CANCELLED"}

        # Validate Aging Tree collections before export
        for col in collections:
            mode = next((e.export_mode for e in context.scene.cs2_collection_ignores
                        if e.name == col.name), "VARIANTS")
            if mode == "AGING_TREE":
                valid, errors = _validate_aging_tree(col)
                if not valid:
                    for err in errors:
                        self.report({"ERROR"}, err)
                    return {"CANCELLED"}

        # Save blend file to temp so background Blender can open it
        tmp_blend = os.path.join(tempfile.gettempdir(), "cs2_export_tmp.blend")
        bpy.ops.wm.save_as_mainfile(filepath=tmp_blend, copy=True)

        # Write export data JSON
        export_data = {
            "items": [
                {
                    "collection":   col.name,
                    "export_mode":  next((e.export_mode for e in context.scene.cs2_collection_ignores if e.name == col.name), "VARIANTS"),
                    "pack_dir":     pack_dir,
                    "tex_size":     int(s.texture_size),
                    "ao_samples":   s.ao_samples,
                    "do_textures":  s.export_textures,
                    "do_decimate":  s.do_decimate,
                    "polys_per_m3": s.polys_per_m3,
                    "max_tris":     s.max_tris,
                }
                for col in collections
            ]
        }
        data_path = os.path.join(tempfile.gettempdir(), "cs2_export_data.json")
        with open(data_path, 'w') as f:
            json.dump(export_data, f)

        self._process = _run_export_in_background(tmp_blend, data_path)
        self._timer   = context.window_manager.event_timer_add(1.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Exporting {len(collections)} collection(s) in background...")
        return {"RUNNING_MODAL"}


# ===========================================================================
# OPEN FOLDER OPERATOR
# ===========================================================================

class CS2_OT_OpenExportFolder(Operator):
    bl_idname      = "cs2.open_export_folder"
    bl_label       = "Open Export Folder"
    bl_description = "Open the export folder in the file explorer"

    def execute(self, context):
        s      = context.scene.cs2_pack_settings
        folder = _resolve_export_folder(context)
        target = os.path.join(folder, s.pack_name) if folder else ""

        if not target or not os.path.exists(target):
            target = folder

        if os.path.exists(target):
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        else:
            self.report({"WARNING"}, "Folder does not exist yet.")
        return {"FINISHED"}


# ===========================================================================
# SYNC OPERATOR
# ===========================================================================

class CS2_OT_CreateAgingTreeStructure(Operator):
    bl_idname      = "cs2.create_aging_tree_structure"
    bl_label       = "Create Aging Tree Structure"
    bl_description = "Create Child/Teen/Adult/Elderly/Dead/Stump sub-collections in the selected collection"

    def execute(self, context):
        # Find the active collection in the outliner
        active_col = context.view_layer.active_layer_collection.collection

        if not active_col or active_col == context.scene.collection:
            self.report({"ERROR"}, "Select a collection in the Outliner first (not Scene Collection).")
            return {"CANCELLED"}

        created = []
        for stage in sorted(AGING_TREE_STAGES):
            if stage not in [c.name for c in active_col.children]:
                new_col = bpy.data.collections.new(stage)
                active_col.children.link(new_col)
                created.append(stage)

        if created:
            self.report({"INFO"}, f"Created: {', '.join(created)} in '{active_col.name}'")
        else:
            self.report({"INFO"}, "All stages already exist.")

        # Set export mode to AGING_TREE for this collection
        _sync_ignore_list(context)
        for entry in context.scene.cs2_collection_ignores:
            if entry.name == active_col.name:
                entry.export_mode = "AGING_TREE"
                break

        return {"FINISHED"}


class CS2_OT_SyncCollections(Operator):
    bl_idname      = "cs2.sync_collections"
    bl_label       = "Refresh"
    bl_description = "Sync collection list"

    def execute(self, context):
        _sync_ignore_list(context)
        return {"FINISHED"}


# ===========================================================================
# SIDEBAR PANEL
# ===========================================================================

class CS2_PT_ExportPanel(Panel):
    bl_label       = "CS2 Asset Exporter"
    bl_idname      = "CS2_PT_export_panel"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "CS2 Export"

    def draw(self, context):
        layout = self.layout
        s      = context.scene.cs2_pack_settings
        prefs  = context.preferences.addons[__name__].preferences

        # ── Pack settings ───────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Pack Settings", icon="PACKAGE")
        box.prop(s, "pack_name")

        # Show resolved export folder
        folder = _resolve_export_folder(context)
        if folder:
            box.label(text=f"→ {folder}", icon="FILE_FOLDER")
        else:
            box.label(text="No export folder set!", icon="ERROR")

        box.prop(s, "export_folder_override", text="Override Folder")
        box.operator("preferences.addon_show",
                     text="Set Default Folder in Preferences",
                     icon="PREFERENCES").module = __name__

        # ── Export options ──────────────────────────────────────────────────
        box2 = layout.box()
        box2.label(text="Export Options", icon="PREFERENCES")
        box2.prop(s, "texture_size")
        row = box2.row()
        row.prop(s, "export_fbx",      icon="EXPORT")
        row.prop(s, "export_textures", icon="IMAGE_DATA")

        if s.export_textures:
            ao = box2.box()
            ao.label(text="AO Bake (Cycles)", icon="LIGHT_SUN")
            ao.prop(s, "ao_samples")

        # ── Decimation ──────────────────────────────────────────────────────
        box3 = layout.box()
        row  = box3.row()
        row.prop(s, "do_decimate", icon="MOD_DECIM")
        if s.do_decimate:
            box3.prop(s, "polys_per_m3")
            box3.prop(s, "max_tris")

        # ── Collections ─────────────────────────────────────────────────────
        box4 = layout.box()
        row  = box4.row()
        row.label(text="Collections", icon="OUTLINER_COLLECTION")
        row.operator("cs2.sync_collections", text="", icon="FILE_REFRESH")

        ig_list = context.scene.cs2_collection_ignores
        if not ig_list:
            box4.label(text="Press ↻ to load", icon="INFO")
        else:
            all_cols = {c.name: c for c in context.scene.collection.children}
            for entry in ig_list:
                col_obj = all_cols.get(entry.name)
                if not col_obj:
                    continue
                mesh_count = sum(1 for o in col_obj.objects if o.type == "MESH")
                col_box = box4.box()
                row = col_box.row(align=True)
                icon = "CHECKBOX_DEHLT" if entry.ignore else "CHECKBOX_HLT"
                row.prop(entry, "ignore", text="", icon=icon, emboss=False)
                if entry.ignore:
                    row.label(text=f"{entry.name}  (ignored)", icon="REMOVE")
                else:
                    row.label(text=entry.name, icon="OBJECT_DATA")
                    row.label(text=f"{mesh_count} mesh(es)")
                    col_box.prop(entry, "export_mode", text="")

        # ── Output preview ──────────────────────────────────────────────────
        if folder and s.pack_name:
            box5 = layout.box()
            box5.label(text="Output structure:", icon="FILEBROWSER")
            box5.label(text=f"{s.pack_name}/", icon="FILE_FOLDER")
            for col in context.scene.collection.children:
                if not any(o.type == "MESH" for o in col.objects):
                    continue
                if _is_ignored(context, col.name):
                    continue
                aname = _sanitize(col.name)
                mode  = next((e.export_mode for e in ig_list if e.name == col.name), "VARIANTS")
                mesh_count = sum(1 for o in col.objects if o.type == "MESH")
                box5.label(text=f"  {aname}/  [{mode}]", icon="OUTLINER_COLLECTION")

                if mode == "AGING_TREE":
                    for child in col.children:
                        stage = f"{aname}Tree{child.name}"
                        icon  = "CHECKMARK" if child.name in AGING_TREE_STAGES else "ERROR"
                        box5.label(text=f"    {stage}/", icon=icon)
                elif mode == "VARIANTS":
                    if mesh_count == 1:
                        box5.label(text=f"    {aname}/  (1 mesh)", icon="OBJECT_DATA")
                    else:
                        for i in range(min(mesh_count, 3)):
                            suffix = chr(ord('a') + i)
                            box5.label(text=f"    {aname}_{suffix}/", icon="OBJECT_DATA")
                        if mesh_count > 3:
                            box5.label(text=f"    ... +{mesh_count-3} more")
                elif mode == "SPLIT_MATERIAL":
                    mats = set()
                    for obj in col.objects:
                        if obj.type == "MESH":
                            for mat in obj.data.materials:
                                if mat: mats.add(_sanitize(mat.name))
                    for mat_name in list(mats)[:3]:
                        box5.label(text=f"    {aname}_{mat_name}.fbx", icon="MATERIAL")
                    if len(mats) > 3:
                        box5.label(text=f"    ... +{len(mats)-3} more")
                elif mode == "SINGLE_MESH":
                    box5.label(text=f"    {aname}/  (merged)", icon="OBJECT_DATA")

        layout.separator()

        # ── Export button ───────────────────────────────────────────────────
        row = layout.row()
        row.scale_y = 1.6
        row.operator("cs2.export_asset_pack", icon="EXPORT")
        layout.operator("cs2.open_export_folder", icon="FILE_FOLDER")


# ===========================================================================
# REGISTRATION
# ===========================================================================

classes = (
    CS2ExporterPreferences,
    CS2CollectionIgnore,
    CS2PackSettings,
    CS2_OT_CreateAgingTreeStructure,
    CS2_OT_SyncCollections,
    CS2_OT_ExportAssetPack,
    CS2_OT_OpenExportFolder,
    CS2_PT_ExportPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cs2_pack_settings      = PointerProperty(type=CS2PackSettings)
    bpy.types.Scene.cs2_collection_ignores = CollectionProperty(type=CS2CollectionIgnore)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.cs2_pack_settings
    del bpy.types.Scene.cs2_collection_ignores


if __name__ == "__main__":
    register()
