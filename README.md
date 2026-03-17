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
Open the CS2 Editor → Asset Importer. For all modes: point the Assets Folder to the **asset root folder** (`assetname/`). CS2 handles the rest automatically.

---

## Export Modes

All modes automatically split meshes by material — one FBX per material slot. The mode only controls the folder structure.

### Variants
Each mesh object gets its own subfolder. Use this for assets with multiple shape variations (e.g. rocks, bushes, plants).

```
MyPack/
└── assetname/                 ← point CS2 here
    ├── assetname_a/
    │   ├── assetname_a_material1.fbx
    │   ├── assetname_a_material1_BaseColor.png
    │   ├── assetname_a_material2.fbx
    │   └── ...
    ├── assetname_b/
    │   ├── assetname_b_material1.fbx
    │   └── settings.json
    └── assetname_c/
        └── ...
```

CS2 imports each subfolder as a separate asset automatically. In the Asset Editor, manually link them as mesh variations via the Object Info Panel.

The first variant exports real textures. All subsequent variants share textures via `settings.json` — no duplicate texture files.

---

### Single Mesh
All mesh objects in the collection are joined, then split by material into one folder.

```
MyPack/
└── assetname/                 ← point CS2 here
    └── assetname/
        ├── assetname_material1.fbx
        ├── assetname_material1_BaseColor.png
        ├── assetname_material2.fbx
        └── ...
```

---

### Aging Tree
For CS2's **Aging Tree** prefab preset. Organise your meshes in sub-collections named after the age stages. The addon auto-creates the sub-collections when you switch to this mode.

**Valid stage names:** `Child` `Teen` `Adult` `Elderly` `Dead` `Stump`

```
Blender Outliner:
└── assetname          ← collection, mode = Aging Tree
    ├── Child          ← sub-collection (auto-created)
    ├── Teen
    ├── Adult
    ├── Elderly
    ├── Dead
    └── Stump
```

Output:
```
MyPack/
└── assetname/                 ← point CS2 here
    ├── assetnameTreeChild/
    │   ├── assetnameTreeChild_material1.fbx
    │   └── textures...
    ├── assetnameTreeAdult/
    └── ...
```

In CS2 Asset Importer: set **Prefab Preset** to **Aging Tree**.

---

## Modifiers

All modifiers that are **enabled in the viewport** (eye icon on) are applied before export. Disabled modifiers are skipped. The export runs on a temporary copy of the blend file so your originals are never touched.

---

## Texture Algorithm

The addon extracts textures from materials using two strategies in order:

### Strategy 1 — Principled BSDF sockets
Standard Blender PBR workflow. Reads directly from the socket inputs of a Principled BSDF node.

### Strategy 2 — Frame label / filename keyword scan
Used when the material has a custom shader (e.g. Poly Haven, PBRPX). Scans all Image Texture nodes and matches them by the **name of their enclosing frame node** or the **image filename**.

| CS2 Slot | Recognized keywords (case-insensitive) |
|----------|---------------------------------------|
| BaseColor | `base color` `basecolor` `albedo` `diffuse` `diff` `color` `_bc` `_d.` |
| Normal | `normal` `nrm` `nor_gl` `nor_dx` `_nor` `_n.` `_nm` |
| Roughness | `roughness` `rough` `rgh` `_r.` `_ro` |
| Metallic | `metallic` `metal` `met` `_m.` `_mt` |

### CS2 MaskMap packing

| Channel | Data |
|---------|------|
| R | Metallic |
| G | Coat (0 = no coat) |
| B | Unused (always black) |
| A | Glossiness (1 − Roughness) |

### Adding custom keywords
Find `kw_map` in `_get_textures()` and extend the lists:

```python
kw_map = {
    "base_color": ["base color", "basecolor", "albedo", ...],
    "normal":     ["normal", "nrm", "nor_gl", ...],
    "roughness":  ["roughness", "rough", ...],
    "metallic":   ["metallic", "metal", ...],
}
```

---

## Scale & Rotation

Scale is handled via `apply_scale_options="FBX_SCALE_NONE"` and `bake_space_transform=True`. Rotation is converted from Blender's Z-up to Unity's Y-up via `axis_forward="-Z", axis_up="Y"`.

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
