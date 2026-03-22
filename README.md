# CS2 Asset Pack Exporter

A Blender addon for exporting asset packs to Cities: Skylines II with correct scale, rotation, texture conversion and folder structure — all in one click.

Built and tested with Blender 5.1 and CS2 patch 1.5.x

## Installation

1. Download `cs2_asset_exporter.py`
2. In Blender: Edit > Preferences > Add-ons > Install
3. Select the `.py` file and enable the addon
4. Set your default export folder in Edit > Preferences > Add-ons > CS2 Asset Exporter

The panel appears in the N-menu (sidebar) of the 3D Viewport under the **CS2 Export** tab.

## Workflow

### 1. Organize your collections
Each top-level collection in the Outliner becomes one asset. The collection name becomes the asset folder name (underscores are automatically removed to comply with the CS2 name parser).

### 2. Set export mode per collection
Press ↻ to sync the collection list, then pick an export mode per collection. You can also configure per-collection options: Force Non-Metallic, Double Sided, and Atlas Regions.

### 3. Export
Click **Export Asset Pack**. The export runs in a background Blender instance so your main Blender stays responsive. Progress is written to `export.log` in your pack folder in real time.

### 4. Import in CS2
Open the CS2 Editor → Asset Importer. For batch imports: point the Assets Folder to the parent folder. CS2 handles the rest automatically.

## Export Modes

### Single Mesh *(default)*
All mesh objects in the collection are joined into one FBX. If the mesh has a single material, the texture names match the FBX name directly. If it has multiple materials, a texture atlas is generated automatically.

```
MyPack/
└── assetname/                 ← point CS2 here
    ├── assetname.fbx
    ├── assetname_BaseColor.png
    ├── assetname_MaskMap.png
    └── ...
```

### Variants
Each mesh object gets its own subfolder. Use this for assets with multiple shape variations (e.g. rocks, bushes, plants). Textures are copied to each variant folder — CS2 batch import requires all textures to be present in each asset folder.

> ⚠️ **Known issue:** Variants mode currently imports with an incorrect material in CS2 (appears chrome/flat). Textures export correctly and the folder structure is valid — the root cause appears to be a CS2 importer behaviour difference between single and batch assets. Use Single Mesh mode as a workaround until resolved.

```
MyPack/
└── assetname/
    ├── assetname_a/
    │   ├── assetname_a.fbx
    │   ├── assetname_a_BaseColor.png
    │   └── ...
    ├── assetname_b/
    │   ├── assetname_b.fbx
    │   ├── assetname_b_BaseColor.png
    │   └── ...
    └── ...
```

### Aging Tree
For CS2's Aging Tree prefab preset. Organise your meshes in sub-collections named after the age stages. The addon auto-creates the sub-collections when you switch to this mode.

Valid stage names: `Child` `Teen` `Adult` `Elderly` `Dead` `Stump`

```
Blender Outliner:
└── assetname          ← collection, mode = Aging Tree
    ├── Child          ← sub-collection (auto-created)
    ├── Teen
    ├── Adult
    ├── Elderly
    ├── Dead
    └── Stump

Output:
MyPack/
└── assetname/
    ├── assetnameTreeChild/
    │   ├── assetnameTreeChild.fbx
    │   └── textures...
    ├── assetnameTreeAdult/
    └── ...
```

In CS2 Asset Importer: set Prefab Preset to **Aging Tree**.

## Per-Collection Options

| Option | Description |
|--------|-------------|
| **Force Non-Metallic** | Forces MaskMap R channel to 0. Use for organic/vegetation assets to prevent chrome appearance. |
| **Double Sided** | Duplicates polygons with flipped normals before export for double-sided rendering in CS2. |
| **Atlas Regions** | Per-material W×H region size for texture atlasing. Only shown when the mesh has multiple material slots. |

## Texture Atlasing

When a mesh has multiple material slots, the addon automatically builds a texture atlas instead of splitting by material. The atlas layout is calculated as a square grid based on total regions. Each material can occupy multiple regions (W×H), configured per material in the UI.

UV coordinates are remapped to fit each material's region. If a material's UVs extend outside the 0–1 range (common with tiled textures like bark or trunk), increase the W or H region size for that material slot — this gives the material more space in the atlas grid so the full UV range is covered and tiles correctly within the region.

## Modifiers

All modifiers that are enabled in the viewport (eye icon on) are applied before export. Disabled modifiers are skipped. The export runs on a temporary copy of the blend file so your originals are never touched.

## Texture Pipeline

The addon extracts textures from materials using two strategies in order:

**Strategy 1 — Principled BSDF sockets**
Standard Blender PBR workflow. Reads directly from the socket inputs of a Principled BSDF node.

**Strategy 2 — Frame label / filename keyword scan**
Used when the material has a custom shader (e.g. Poly Haven, PBRPX). Scans all Image Texture nodes and matches them by the name of their enclosing frame node or the image filename.

| CS2 Slot | Recognized keywords (case-insensitive) |
|----------|----------------------------------------|
| BaseColor | base color basecolor albedo diffuse diff color _bc _d. |
| Normal | normal nrm nor_gl nor_dx _nor _n. _nm |
| Roughness | roughness rough rgh _r. _ro |
| Metallic | metallic metal met _m. _mt |
| Alpha | alpha opacity _a. _op mask cutout transmission |

## CS2 MaskMap packing

| Channel | Data |
|---------|------|
| R | Metallic (0 for organic assets) |
| G | Coat (0 = no coat) |
| B | Unused (always black) |
| A | Glossiness (1 − Roughness) |

`ControlMask` exports as pure black by default — prevents CS2 from applying brown dirt color variations.

## Export Log & Timeout

Every export writes a live `export.log` to your pack folder. A configurable timeout (default 120s, 0 = disabled) kills the worker if it hangs and logs the last known state.

## Scale & Rotation

Scale is handled via `apply_scale_options="FBX_SCALE_NONE"` and `bake_space_transform=True`. Rotation is converted from Blender's Z-up to Unity's Y-up via `axis_forward="-Z"`, `axis_up="Y"`.

## Asset Sources

The addon is designed to work with assets from:
- [Poly Haven](https://polyhaven.com) — CC0, free to redistribute
- [PBRPX](https://pbrpx.com) — CC0, free to redistribute
- [Fab.com](https://fab.com) — check license before redistributing with your mod
- Custom Blender assets with Principled BSDF materials

## License

MIT — free to use, modify and distribute.

## Contributing

Pull requests welcome. If you find a texture naming convention that is not recognized, open an issue with the material node setup and it will be added to the keyword list.

## Changelog

### v2.3.0
- Added texture atlasing for multi-material meshes with configurable per-material region sizes
- Added Force Non-Metallic toggle per collection
- Added Double Sided mesh toggle per collection
- Added live export logging to `export.log` with configurable timeout
- ControlMask now exports as pure black (prevents brown dirt override in CS2)
- BaseColor alpha channel now correctly packed from alpha texture
- MaskMap B channel forced to 0.0 per CS2 spec
- Single Mesh now exports directly to collection folder without extra subfolder
- Variants now copy textures to each variant folder (required for CS2 batch import)
- FBX and texture names no longer include material name for single-material assets
- UV remapping rewritten with numpy for performance
- Removed AO bake system
- Removed Split per Material mode (replaced by atlasing)
- Fixed shutil.rmtree PermissionError on .fbm folder deletion
- Fixed .001 material suffix mismatch in atlas entry lookup
- Known issue: Variants mode imports with incorrect material in CS2 — use Single Mesh as workaround
- Known issue: Atlas region sizes may need manual adjustment for tiled UVs outside 0–1 range

### v2.2.0
- Initial public release
