bl_info = {
    "name": "CS2 Asset Pack Exporter",
    "author": "CS2 Modding Tool",
    "version": (2, 2, 0),
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
import shutil
import numpy as np
from bpy.props import (
    StringProperty, BoolProperty, EnumProperty,
    IntProperty, FloatProperty, PointerProperty,
    CollectionProperty,
)
from bpy.types import Panel, Operator, PropertyGroup, AddonPreferences


# ===========================================================================
# ADDON PREFERENCES
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
        layout.label(text="This folder is used when no pack-specific folder is set.", icon="INFO")


# ===========================================================================
# PER-COLLECTION IGNORE LIST
# ===========================================================================

def _on_export_mode_update(self, context):
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
# SCENE SETTINGS
# ===========================================================================

class CS2PackSettings(PropertyGroup):
    pack_name: StringProperty(name="Pack Name", description="Name of the asset pack (output folder name)", default="MyAssetPack")
    export_folder_override: StringProperty(name="Export Folder (override)", description="Leave empty to use the default folder from addon preferences", default="", subtype="DIR_PATH")
    texture_size: EnumProperty(
        name="Texture Size",
        items=[("512","512 px",""),("1024","1024 px",""),("2048","2048 px","Recommended"),("4096","4096 px","Max CS2")],
        default="2048",
    )
    export_fbx: BoolProperty(name="Export FBX", default=True)
    export_textures: BoolProperty(name="Export Textures", default=True)
    do_decimate: BoolProperty(name="Auto Decimate", default=False)
    polys_per_m3: FloatProperty(name="Polys / m³", default=2000.0, min=10.0, max=500000.0, step=100)
    max_tris: IntProperty(name="Max Triangles", default=10000, min=100, max=200000, step=500)


def _resolve_export_folder(context):
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
    base = re.sub(r'_?LOD\d+$', '', name, flags=re.IGNORECASE)
    return base.replace('_', '')


# ===========================================================================
# AGING TREE VALIDATION
# ===========================================================================

AGING_TREE_STAGES = {"Child", "Teen", "Adult", "Elderly", "Dead", "Stump"}


def _base_stage_name(name):
    return re.sub(r'\.\d+$', '', name)


def _validate_aging_tree(collection):
    errors = []
    if not list(collection.children):
        errors.append(f"'{collection.name}' has no child collections.")
        return False, errors
    for col in collection.children:
        if _base_stage_name(col.name) not in AGING_TREE_STAGES:
            errors.append(f"Invalid stage name '{col.name}'. Must be one of: {', '.join(sorted(AGING_TREE_STAGES))}")
    return len(errors) == 0, errors


# ===========================================================================
# TEXTURE EXTRACTION (used by worker via exec)
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
    bsdf = _get_principled(material)
    if bsdf:
        for key, sock in [("base_color","Base Color"),("roughness","Roughness"),("metallic","Metallic"),("normal","Normal")]:
            s = bsdf.inputs.get(sock)
            if s:
                result[key] = _image_from_socket(s)
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


# ===========================================================================
# TEXTURE SAVING (used by worker via exec)
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


def _save_textures(textures, asset_name, asset_dir, tex_size):
    w = h = tex_size
    if textures["base_color"]:
        _save_png(_px_to_np(textures["base_color"], w, h), os.path.join(asset_dir, f"{asset_name}_BaseColor.png"), w, h)
    if textures["normal"]:
        _save_png(_px_to_np(textures["normal"], w, h), os.path.join(asset_dir, f"{asset_name}_Normal.png"), w, h)
    # MaskMap: R=Metallic, G=Coat(0), B=Black(unused), A=Glossiness(1-Roughness)
    mask = np.zeros((h, w, 4), dtype=np.float32)
    mask[:,:,0] = _px_to_np(textures["metallic"], w, h)[:,:,0] if textures["metallic"] else 0.0
    mask[:,:,1] = 0.0
    mask[:,:,2] = 0.0
    mask[:,:,3] = (1.0 - _px_to_np(textures["roughness"], w, h)[:,:,0]) if textures["roughness"] else 0.5
    _save_png(mask, os.path.join(asset_dir, f"{asset_name}_MaskMap.png"), w, h)
    _save_png(np.ones((h, w, 4), dtype=np.float32), os.path.join(asset_dir, f"{asset_name}_ControlMask.png"), w, h)


# ===========================================================================
# DECIMATION (used by worker via exec)
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
# FBX EXPORT (used by worker via exec)
# ===========================================================================

def _export_fbx(obj, fbx_path):
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
    fbm_path = os.path.splitext(fbx_path)[0] + ".fbm"
    if os.path.isdir(fbm_path):
        shutil.rmtree(fbm_path)
    obj.hide_viewport = orig_hide
    bpy.ops.object.select_all(action="DESELECT")


# ===========================================================================
# BACKGROUND EXPORT WORKER
# ===========================================================================

def _run_export_in_background(blend_path, export_data_path):
    import time
    worker_script = os.path.join(tempfile.gettempdir(), f"cs2_export_worker_{int(time.time())}.py")

    script_content = f"""
import bpy, sys, os, json, re, numpy as np, tempfile, shutil

with open(r"{export_data_path}") as f:
    data = json.load(f)

exec(open(r"{os.path.abspath(__file__)}").read())

context_dummy = bpy.context

# Apply enabled modifiers on every mesh
for obj in bpy.data.objects:
    if obj.type != "MESH":
        continue
    for mod in list(obj.modifiers):
        if not mod.show_viewport:
            continue
        try:
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.ops.object.modifier_apply(modifier=mod.name)
            obj.select_set(False)
        except Exception as e:
            print(f"WARNING: Could not apply modifier '{{mod.name}}' on '{{obj.name}}': {{e}}")
            obj.select_set(False)

for item in data["items"]:
    col_name    = item["collection"]
    pack_dir    = item["pack_dir"]
    tex_size    = item["tex_size"]
    do_tex      = item["do_textures"]
    do_fbx      = item["do_fbx"]
    do_dec      = item["do_decimate"]
    polys_m3    = item["polys_per_m3"]
    max_tris    = item["max_tris"]
    export_mode = item.get("export_mode", "VARIANTS")

    col = bpy.data.collections.get(col_name)
    if not col:
        print(f"WARNING: collection {{col_name}} not found")
        continue

    mesh_objects = [o for o in col.objects if o.type == "MESH"]
    asset_name   = _sanitize(col_name)
    col_dir      = os.path.join(pack_dir, asset_name)
    os.makedirs(col_dir, exist_ok=True)

    if do_dec:
        for obj in mesh_objects:
            _decimate(obj, polys_m3, max_tris, print)

    def _export_mesh_mats(obj, folder, base_name, do_fbx, do_tex, tex_size):
        mats = [m for m in obj.data.materials if m]
        if not mats:
            if do_fbx:
                print(f"PROGRESS: {{base_name}} (no materials)")
                _export_fbx(obj, os.path.join(folder, f"{{base_name}}.fbx"))
            return
        for mat in mats:
            safe_mat = _sanitize(mat.name)
            fbx_name = f"{{base_name}}_{{safe_mat}}"
            print(f"PROGRESS: {{fbx_name}}")
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.duplicate()
            d = bpy.context.active_object
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="DESELECT")
            d.active_material_index = d.data.materials.find(mat.name)
            bpy.ops.object.material_slot_select()
            bpy.ops.mesh.select_all(action="INVERT")
            bpy.ops.mesh.delete(type="FACE")
            bpy.ops.object.mode_set(mode="OBJECT")
            d.name = fbx_name
            if do_fbx:
                _export_fbx(d, os.path.join(folder, f"{{fbx_name}}.fbx"))
            if do_tex:
                _save_textures(_get_textures(mat), fbx_name, folder, tex_size)
            bpy.ops.object.select_all(action="DESELECT")
            d.select_set(True)
            bpy.ops.object.delete()

    # ── VARIANTS ─────────────────────────────────────────────────────────
    if export_mode == "VARIANTS":
        tex_source_name = None
        for idx, obj in enumerate(mesh_objects):
            var_name = asset_name if len(mesh_objects) == 1 else f"{{asset_name}}_{{chr(ord('a')+idx)}}"
            var_dir  = os.path.join(col_dir, var_name)
            os.makedirs(var_dir, exist_ok=True)
            if idx == 0 or len(mesh_objects) == 1:
                _export_mesh_mats(obj, var_dir, var_name, do_fbx, do_tex, tex_size)
                tex_source_name = var_name
            else:
                _export_mesh_mats(obj, var_dir, var_name, do_fbx, False, tex_size)
                if do_tex and tex_source_name:
                    mats = [m for m in obj.data.materials if m]
                    shared = {{}}
                    if mats:
                        for mat in mats:
                            sm = _sanitize(mat.name)
                            for slot in ["BaseColor", "Normal", "MaskMap", "ControlMask"]:
                                shared[f"{{var_name}}_{{sm}}_{{slot}}.png"] = f"../{{tex_source_name}}/{{tex_source_name}}_{{sm}}_{{slot}}.png"
                    else:
                        for slot in ["BaseColor", "Normal", "MaskMap", "ControlMask"]:
                            shared[f"{{var_name}}_{{slot}}.png"] = f"../{{tex_source_name}}/{{tex_source_name}}_{{slot}}.png"
                    with open(os.path.join(var_dir, "settings.json"), "w") as f:
                        json.dump({{"sharedAssets": shared}}, f, indent=2)

    # ── SINGLE MESH ───────────────────────────────────────────────────────
    elif export_mode == "SINGLE_MESH":
        bpy.ops.object.select_all(action="DESELECT")
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_objects[0]
        bpy.ops.object.duplicate()
        bpy.ops.object.join()
        joined = bpy.context.active_object
        joined.name = asset_name
        var_dir = os.path.join(col_dir, asset_name)
        os.makedirs(var_dir, exist_ok=True)
        _export_mesh_mats(joined, var_dir, asset_name, do_fbx, do_tex, tex_size)
        bpy.ops.object.select_all(action="DESELECT")
        joined.select_set(True)
        bpy.ops.object.delete()

    # ── SPLIT PER MATERIAL ────────────────────────────────────────────────
    elif export_mode == "SPLIT_MATERIAL":
        bpy.ops.object.select_all(action="DESELECT")
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_objects[0]
        bpy.ops.object.duplicate()
        bpy.ops.object.join()
        joined = bpy.context.active_object
        joined.name = asset_name
        _export_mesh_mats(joined, col_dir, asset_name, do_fbx, do_tex, tex_size)
        bpy.ops.object.select_all(action="DESELECT")
        joined.select_set(True)
        bpy.ops.object.delete()

    # ── AGING TREE ────────────────────────────────────────────────────────
    if export_mode == "AGING_TREE":
        aging_stages = {{"Child", "Teen", "Adult", "Elderly", "Dead", "Stump"}}
        def _base(n):
            import re as _re2
            return _re2.sub(r'\\.\\d+$', '', n)
        for child_col in col.children:
            if _base(child_col.name) not in aging_stages:
                continue
            stage_meshes = [o for o in child_col.objects if o.type == "MESH"]
            if not stage_meshes:
                continue
            stage_name = f"{{asset_name}}Tree{{_base(child_col.name)}}"
            stage_dir  = os.path.join(col_dir, stage_name)
            os.makedirs(stage_dir, exist_ok=True)
            if do_dec:
                for obj in stage_meshes:
                    _decimate(obj, polys_m3, max_tris, print)
            if len(stage_meshes) == 1:
                _export_mesh_mats(stage_meshes[0], stage_dir, stage_name, do_fbx, do_tex, tex_size)
            else:
                bpy.ops.object.select_all(action="DESELECT")
                for obj in stage_meshes:
                    obj.select_set(True)
                bpy.context.view_layer.objects.active = stage_meshes[0]
                bpy.ops.object.duplicate()
                bpy.ops.object.join()
                joined = bpy.context.active_object
                joined.name = stage_name
                _export_mesh_mats(joined, stage_dir, stage_name, do_fbx, do_tex, tex_size)
                bpy.ops.object.select_all(action="DESELECT")
                joined.select_set(True)
                bpy.ops.object.delete()
            print(f"  Stage done: {{stage_name}}")

    print(f"Done: {{col_name}} ({{export_mode}})")

print("EXPORT_COMPLETE")
"""
    with open(worker_script, 'w') as f:
        f.write(script_content)

    cmd = [bpy.app.binary_path, "--background", blend_path, "--python", worker_script]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


# ===========================================================================
# MAIN EXPORT OPERATOR
# ===========================================================================

class CS2_OT_ExportAssetPack(Operator):
    bl_idname      = "cs2.export_asset_pack"
    bl_label       = "Export Asset Pack"
    bl_description = "Export all collections as CS2-ready asset packs (background process)"

    _process = None
    _timer   = None

    def modal(self, context, event):
        if event.type == "TIMER":
            if self._process.poll() is not None:
                output = self._process.stdout.read()
                context.scene.cs2_export_running = False
                if "EXPORT_COMPLETE" in output:
                    self.report({"INFO"}, "CS2 Export completed!")
                else:
                    self.report({"WARNING"}, "Export finished with warnings — check console.")
                    print(output)
                context.window_manager.event_timer_remove(self._timer)
                context.workspace.status_text_set(None)
                return {"FINISHED"}
            context.workspace.status_text_set("CS2 Export running in background...")
        return {"PASS_THROUGH"}

    def cancel(self, context):
        if self._process and self._process.poll() is None:
            self._process.terminate()
        context.window_manager.event_timer_remove(self._timer)
        context.workspace.status_text_set(None)
        context.scene.cs2_export_running = False
        self.report({"WARNING"}, "CS2 Export cancelled.")

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

        for col in collections:
            mode = next((e.export_mode for e in context.scene.cs2_collection_ignores if e.name == col.name), "VARIANTS")
            if mode == "AGING_TREE":
                valid, errors = _validate_aging_tree(col)
                if not valid:
                    for err in errors:
                        self.report({"ERROR"}, err)
                    return {"CANCELLED"}

        tmp_blend = os.path.join(tempfile.gettempdir(), "cs2_export_tmp.blend")
        bpy.ops.wm.save_as_mainfile(filepath=tmp_blend, copy=True)

        export_data = {
            "items": [
                {
                    "collection":   col.name,
                    "export_mode":  next((e.export_mode for e in context.scene.cs2_collection_ignores if e.name == col.name), "VARIANTS"),
                    "pack_dir":     pack_dir,
                    "tex_size":     int(s.texture_size),
                    "do_textures":  s.export_textures,
                    "do_fbx":       s.export_fbx,
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
        context.scene.cs2_export_running = True
        self.report({"INFO"}, f"Exporting {len(collections)} collection(s) in background...")
        return {"RUNNING_MODAL"}


# ===========================================================================
# CANCEL EXPORT OPERATOR
# ===========================================================================

class CS2_OT_CancelExport(Operator):
    bl_idname      = "cs2.cancel_export"
    bl_label       = "Cancel Export"
    bl_description = "Cancel the running background export"

    def execute(self, context):
        for op in context.window_manager.operators:
            if op.bl_idname == "CS2_OT_export_asset_pack":
                if hasattr(op, "_process") and op._process and op._process.poll() is None:
                    op._process.terminate()
        context.scene.cs2_export_running = False
        context.workspace.status_text_set(None)
        self.report({"WARNING"}, "CS2 Export cancelled.")
        return {"FINISHED"}


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

        box = layout.box()
        box.label(text="Pack Settings", icon="PACKAGE")
        box.prop(s, "pack_name")
        folder = _resolve_export_folder(context)
        if folder:
            box.label(text=f"→ {folder}", icon="FILE_FOLDER")
        else:
            box.label(text="No export folder set!", icon="ERROR")
        box.prop(s, "export_folder_override", text="Override Folder")
        box.operator("preferences.addon_show", text="Set Default Folder in Preferences", icon="PREFERENCES").module = __name__

        box2 = layout.box()
        box2.label(text="Export Options", icon="PREFERENCES")
        box2.prop(s, "texture_size")
        row = box2.row()
        row.prop(s, "export_fbx",      icon="EXPORT")
        row.prop(s, "export_textures", icon="IMAGE_DATA")

        box3 = layout.box()
        row  = box3.row()
        row.prop(s, "do_decimate", icon="MOD_DECIM")
        if s.do_decimate:
            box3.prop(s, "polys_per_m3")
            box3.prop(s, "max_tris")

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
                        ico   = "CHECKMARK" if child.name in AGING_TREE_STAGES else "ERROR"
                        box5.label(text=f"    {stage}/", icon=ico)
                elif mode == "VARIANTS":
                    if mesh_count == 1:
                        box5.label(text=f"    {aname}/  (1 mesh)", icon="OBJECT_DATA")
                    else:
                        for i in range(min(mesh_count, 3)):
                            box5.label(text=f"    {aname}_{chr(ord('a')+i)}/", icon="OBJECT_DATA")
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
        row = layout.row()
        row.scale_y = 1.6
        if context.scene.get("cs2_export_running"):
            row.operator("cs2.cancel_export", icon="X", text="Cancel Export")
        else:
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
    CS2_OT_CancelExport,
    CS2_OT_OpenExportFolder,
    CS2_PT_ExportPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cs2_pack_settings      = PointerProperty(type=CS2PackSettings)
    bpy.types.Scene.cs2_collection_ignores = CollectionProperty(type=CS2CollectionIgnore)
    bpy.types.Scene.cs2_export_running     = BoolProperty(default=False)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.cs2_pack_settings
    del bpy.types.Scene.cs2_collection_ignores
    del bpy.types.Scene.cs2_export_running


if __name__ == "__main__":
    register()
