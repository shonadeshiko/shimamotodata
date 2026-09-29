"""
島本町の実データを取り込むパイプライン。

- ラスタ: EPSG:4326に再投影し、COG(Cloud Optimized GeoTIFF)に変換
- ベクタ: EPSG:4326に変換し、GeoJSONとして書き出し（フロントで直接fetchできる）
- 生成物は data/processed/catalog.json (ラスタ) と
  data/processed/vectors_catalog.json (ベクタ) に登録される

RASTER_DEFS / VECTOR_DEFS にファイルを追加するだけで、
新しい島本町データを取り込めるようにしてある。
元データは data/raw/shimamoto/{raster,vector}/ に配置する（Git管理外）。

このファイルは gisdata(千葉県版)の ingest_chiba_data.py をベースに
地域名だけ置き換えた雛形。実データが揃い次第、RASTER_DEFS / VECTOR_DEFS に
定義を追加していく。
"""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "shimamoto"
PROCESSED_DIR = Path(__file__).resolve().parents[2] / "data" / "processed"
RASTERS_DIR = PROCESSED_DIR / "rasters"
VECTORS_DIR = PROCESSED_DIR / "vectors"

DST_CRS = "EPSG:4326"

# id, 元ファイル名, 表示名, 単位, 説明, リサンプリング方法
RASTER_DEFS: list[dict] = [
    {
        "id": "shimamoto_paddy_ratio",
        "src": "05_水田の占有率_27_大阪府.tif",
        "name": "水田の占有率（大阪府）",
        "unit": "比率(0-1)",
        "description": "グリッド内における水田の占有割合。値が高いほど水田が多い。大阪府域全体のデータ。",
        "resampling": Resampling.bilinear,
    },
    {
        "id": "shimamoto_hand_rank",
        "src": "HANDランク_島本町.tif",
        "name": "HANDランク（島本町）",
        "unit": "ランク(1-5)",
        "description": "最近接水路との比高(HAND)による区分。値が大きいほど水路との比高が小さい(水路に近い)。",
        "resampling": Resampling.nearest,
    },
    {
        "id": "shimamoto_dev_pressure_2020_2024",
        "src": "開発圧v2_2020-2024_島本町.tif",
        "name": "開発圧 2020-2024 v2（島本町）",
        "unit": "区分(-1,0,+1)",
        "description": "2020年から2024年にかけての開発圧の変化区分(v2データ)。+1:都市化(開発圧増加) 0:変化なし -1:開発後退(緑地化等)。",
        "resampling": Resampling.nearest,
    },
    {
        "id": "shimamoto_dev_pressure_2011_2022",
        "src": "開発圧v2_2011-2022_島本町.tif",
        "name": "開発圧 2011-2022 v2（島本町）",
        "unit": "区分(-1,0,+1)",
        "description": "2011年から2022年にかけての開発圧の変化区分(v2データ)。+1:都市化(開発圧増加) 0:変化なし -1:開発後退(緑地化等)。",
        "resampling": Resampling.nearest,
    },
    {
        "id": "shimamoto_twi_rank",
        "src": "TWIランク_島本町.tif",
        "name": "TWIランク（島本町）",
        "unit": "ランク(1-5)",
        "description": "地形的湿潤度指数(TWI)に基づく浸水・湛水しやすさの目安ランク。値が大きいほど水が集まりやすい地形。",
        "resampling": Resampling.nearest,
    },
    {
        "id": "shimamoto_gi_terrain_score",
        "src": "GI地形スコア_島本町.tif",
        "name": "GI地形スコア（島本町）",
        "unit": "スコア(1.0-5.0)",
        "description": "地形条件から見たグリーンインフラ(GI)適性の統合スコア(連続値)。",
        "resampling": Resampling.bilinear,
        "uint8_scale": 10,  # 10倍してuint8化(小数点以下1桁の精度を保持)。frontendで10で割り戻す
    },
]

# id, 元ファイル名, 表示名, 説明
VECTOR_DEFS: list[dict] = [
    {
        "id": "shimamoto_boundary",
        "src": "島本町域_行政界+1kmバッファ.gpkg",
        "name": "島本町域（行政界+1kmバッファ）",
        "description": "島本町の行政界に1kmのバッファを加えた範囲のポリゴン。",
    },
    {
        "id": "shimamoto_mesh500m_gi",
        "src": "メッシュ500m_GI統合v2_島本町.gpkg",
        "name": "500mメッシュ GI統合スコア（島本町）",
        "description": "500mメッシュ単位のグリーンインフラ(GI)関連スコア・開発圧・土地被覆割合等の統合データ(v2)。",
    },
]


def reproject_to_cog(
    src_path: Path,
    dst_cog_path: Path,
    resampling: Resampling,
    uint8_scale: float | None = None,
) -> None:
    """
    uint8_scale を指定すると、値をscale倍してuint8(0-255)に丸めて保存する
    (例: 1.0-5.0の連続値をscale=10で保存すると10-50のuint8になり、
    frontend側でscaleで割り戻すことで小数点以下1桁の精度を保ったまま軽量化できる)。
    整数ランクなど元々小さい整数値のデータは uint8_scale=1 を指定する。
    """
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, DST_CRS, src.width, src.height, *src.bounds
        )
        kwargs = src.meta.copy()
        kwargs.update(
            {
                "crs": DST_CRS,
                "transform": transform,
                "width": width,
                "height": height,
            }
        )
        if uint8_scale is not None:
            kwargs["dtype"] = "float32"

        tmp_path = dst_cog_path.with_suffix(".reproj.tif")
        dst_cog_path.parent.mkdir(parents=True, exist_ok=True)

        with rasterio.open(tmp_path, "w", **kwargs) as dst:
            for band in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=DST_CRS,
                    resampling=resampling,
                )

    if uint8_scale is not None:
        with rasterio.open(tmp_path) as src:
            data = src.read(1)
            nodata_mask = np.isnan(data) if src.nodata is None else (data == src.nodata)
            scaled = np.clip(np.round(data * uint8_scale), 0, 255).astype("uint8")
            scaled[nodata_mask] = 0

            uint8_profile = src.profile.copy()
            uint8_profile.update({"dtype": "uint8", "nodata": 0})
            uint8_path = dst_cog_path.with_suffix(".uint8.tif")
            with rasterio.open(uint8_path, "w", **uint8_profile) as dst:
                dst.write(scaled, 1)
        tmp_path.unlink()
        tmp_path = uint8_path

    # ブラウザ側はCOGを全体取得してから自前でウィンドウ読み込みする方式のため、
    # ズームレベル別のオーバービュー(ピラミッド)は使わない。生成すると
    # ファイルサイズが3割前後増えるだけなので、overview_level=0で無効化する。
    cog_translate(
        str(tmp_path),
        str(dst_cog_path),
        cog_profiles.get("deflate"),
        overview_level=0,
        in_memory=False,
        quiet=True,
    )
    tmp_path.unlink()


def process_rasters() -> list[dict]:
    catalog = []
    for definition in RASTER_DEFS:
        src_path = RAW_DIR / "raster" / definition["src"]
        if not src_path.exists():
            print(f"スキップ（未取得）: {src_path}")
            continue

        dst_path = RASTERS_DIR / f"{definition['id']}.tif"
        print(f"処理中: {definition['src']} -> {dst_path.name}")
        uint8_scale = definition.get("uint8_scale")
        reproject_to_cog(src_path, dst_path, definition["resampling"], uint8_scale)

        entry = {
            "id": definition["id"],
            "name": definition["name"],
            "unit": definition["unit"],
            "description": definition["description"],
            "path": f"rasters/{definition['id']}.tif",
        }
        if uint8_scale is not None:
            entry["scale"] = uint8_scale
        catalog.append(entry)
        print(f"完了: {dst_path}")
    return catalog


def process_vectors() -> list[dict]:
    catalog = []
    for definition in VECTOR_DEFS:
        src_path = RAW_DIR / "vector" / definition["src"]
        if not src_path.exists():
            print(f"スキップ（未取得）: {src_path}")
            continue

        dst_path = VECTORS_DIR / f"{definition['id']}.geojson"
        print(f"処理中: {definition['src']} -> {dst_path.name}")

        gdf = gpd.read_file(src_path)
        if gdf.crs is not None and gdf.crs.to_string() != DST_CRS:
            gdf = gdf.to_crs(DST_CRS)

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(dst_path, driver="GeoJSON")

        catalog.append(
            {
                "id": definition["id"],
                "name": definition["name"],
                "description": definition["description"],
                "path": f"vectors/{definition['id']}.geojson",
                "feature_count": int(len(gdf)),
            }
        )
        print(f"完了: {dst_path} ({len(gdf)}件)")
    return catalog


def merge_catalog(existing_path: Path, new_entries: list[dict], key: str) -> None:
    if existing_path.exists():
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    else:
        existing = {key: []}

    existing_ids = {item["id"] for item in existing.get(key, [])}
    for entry in new_entries:
        if entry["id"] not in existing_ids:
            existing.setdefault(key, []).append(entry)
        else:
            existing[key] = [entry if item["id"] == entry["id"] else item for item in existing[key]]

    existing_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    raster_entries = process_rasters()
    vector_entries = process_vectors()

    merge_catalog(PROCESSED_DIR / "catalog.json", raster_entries, "rasters")
    merge_catalog(PROCESSED_DIR / "vectors_catalog.json", vector_entries, "vectors")

    print("\nカタログ更新完了")


if __name__ == "__main__":
    main()
