# CS2 Asset Pack Exporter

A Blender addon for exporting asset packs to **Cities: Skylines II** with correct scale, rotation, texture conversion and folder structure — all in one click.

> Built and tested with Blender 5.0 and CS2 patch 1.5.x

---

## Installation

1. Download `cs2_asset_exporter.py`
2. In Blender: **Edit > Preferences > Add-ons > Install**
3. Select the `.py` file and enable the addon
4. Set your default export folder in **Edit > Preferences > Add-ons > CS2 Asset Exporter**

The panel appears in the **N-menu (sidebar)** of the 3D Viewport under the **CS2 Export** tab.

---

## Workflow

### 1. Organize your collections
Each top-level collection in the Outliner becomes one asset. The collection name becomes the asset folder name (underscores are automatically removed to comply with the CS2 name parser).

### 2. Set export mode per collection
Press **↻** to sync the collection list, then pick an export mode per collection.

### 3. Export
Click **Export Asset Pack**. The export runs in a **background Blender instance** so your main Blender stays responsive.

### 4. Import in CS2
Open the CS2 Editor → Asset Importer. Set **Project Root** to your pack folder and **Assets Folder** to the asset subfolder.

---

## Export Modes

### Variants
Each mesh object in the collection gets its own subfolder. Use this for assets with multiple shape variations (e.g. rocks, bushes).

```
MyRockPack/
└── boulder01/
    ├── boulder01_a/
    │   ├── boulder01_a.fbx
    │   ├── boulder01_a_BaseColor.png
    │   └── ...
    └── boulder01_b/
        └── ...
```

Import each subfolder separately in CS2. Then manually link them as mesh variations in the Asset Editor via the Object Info Panel.

---

### Single Mesh
All mesh objects in the collection are joined into one FBX. Use this for simple props with a single material.

```
MyRockPack/
└── boulder01/
    └── boulder01/
        ├── boulder01.fbx
        └── textures...
```

---

### Split per Material
One FBX per material slot, all in the same folder. CS2 combines them into one mesh on import. Use this for assets with multiple materials (e.g. a tree with separate bark/leaves/branches materials).

```
MyTreePack/
└── jacarandatree/
    ├── jacarandatree_branches.fbx
    ├── jacarandatree_trunk.fbx
    ├── jacarandatree_leaves.fbx
    ├── jacarandatree_branches_BaseColor.png
    ├── jacarandatree_trunk_BaseColor.png
    └── ...
```

---

### Aging Tree
For CS2's **Aging Tree** prefab preset. Organise your meshes in sub-collections named after the age stages. The addon auto-creates the sub-collections when you switch to this mode.

**Valid stage names:** `Child` `Teen` `Adult` `Elderly` `Dead` `Stump`

```
Blender Outliner:
└── quivertree02          ← collection, mode = Aging Tree
    ├── Child             ← sub-collection (auto-created)
    ├── Teen
    ├── Adult
    ├── Elderly
    ├── Dead
    └── Stump
```

Output:
```
MyTreePack/
└── quivertree02/
    ├── quivertree02TreeChild/
    │   ├── quivertree02TreeChild.fbx
    │   └── textures...
    ├── quivertree02TreeAdult/
    └── ...
```

In CS2 Asset Importer: set **Prefab Preset** to **Aging Tree** and point the Assets Folder to `quivertree02/`.

---

## Texture Algorithm

The addon tries to extract textures from materials using two strategies in order:

### Strategy 1 — Principled BSDF sockets
Standard Blender PBR workflow. Reads directly from the socket inputs of a Principled BSDF node.

| CS2 Slot | Principled BSDF Socket |
|----------|----------------------|
| BaseColor | Base Color |
| Normal | Normal |
| MaskMap R | Metallic |
| MaskMap A (Smoothness) | 1 − Roughness |
| MaskMap G (AO) | Baked via Cycles |

### Strategy 2 — Frame label / filename keyword scan
Used when the material has a custom shader (e.g. Poly Haven, PBRPX). Scans all Image Texture nodes and matches them by the **name of their enclosing frame node** or the **image filename**.

| CS2 Slot | Recognized keywords (case-insensitive) |
|----------|---------------------------------------|
| BaseColor | `base color` `basecolor` `albedo` `diffuse` `diff` `color` `_bc` `_d.` |
| Normal | `normal` `nrm` `nor_gl` `nor_dx` `_nor` `_n.` `_nm` |
| Roughness | `roughness` `rough` `rgh` `_r.` `_ro` |
| Metallic | `metallic` `metal` `met` `_m.` `_mt` |

### CS2 MaskMap packing
CS2 uses a packed texture for PBR data:

| Channel | Data |
|---------|------|
| R | Metallic |
| G | Ambient Occlusion (baked via Cycles) |
| B | Detail mask (always 1.0) |
| A | Smoothness (1 − Roughness) |

### Adding custom keywords
If your asset library uses different naming conventions, you can extend the keyword lists directly in the script — find `kw_map` in `_get_textures()`:

```python
kw_map = {
    "base_color": ["base color", "basecolor", "albedo", "diffuse", "diff", ...],
    "normal":     ["normal", "nrm", "nor_gl", ...],
    "roughness":  ["roughness", "rough", ...],
    "metallic":   ["metallic", "metal", ...],
}
```

---

## Scale & Rotation

CS2 runs on Unity which applies a **0.01 import scale factor** to FBX files (treats units as centimeters). The addon compensates by physically scaling the mesh 100x before export and restoring it afterward, so a 1m object in Blender arrives as 1m in CS2.

Rotation is handled via `bake_space_transform=True` with `axis_forward="-Z", axis_up="Y"`, converting Blender's Z-up coordinate system to Unity's Y-up.

---

## AO Baking

Ambient Occlusion is baked automatically via **Cycles** before texture export. You can control the quality with the **AO Samples** setting:

| Samples | Quality |
|---------|---------|
| 4-16 | Fast preview |
| 32-64 | Good |
| 128+ | High quality |

---

## Asset Sources

The addon is designed to work with assets from:

- **[Poly Haven](https://polyhaven.com)** — CC0, free to redistribute
- **[PBRPX](https://pbrpx.com)** — CC0, free to redistribute
- **[Fab.com](https://fab.com)** — check license before redistributing with your mod
- Custom Blender assets with Principled BSDF materials

---

## License

MIT — free to use, modify and distribute.

---

## Contributing

Pull requests welcome. If you find a texture naming convention that is not recognized, open an issue with the material node setup and it will be added to the keyword list.
