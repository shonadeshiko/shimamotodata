"""
国土地理院(GSI)の標高タイルを取得し、島本町周辺のDEM(標高)ラスタを作る。

標高タイル(DEM10B, 10mメッシュ, 全国カバー)を該当エリア分だけダウンロードし、
Web Mercator(EPSG:3857)でモザイク結合したのち、EPSG:4326に再投影してCOG化する。
ファイルサイズを抑えるため、zoom13(約20m/px)・int16(m単位,小数切り捨て)で出力する。

参考: 国土地理院 標高タイル
https://maps.gsi.go.jp/development/ichiran.html
タイルURL: https://cyberjapandata.gsi.go.jp/xyz/dem/{z}/{x}/{y}.txt
（各タイルは256x256のCSVで、"e"はデータなし、単位はm）

このスクリプトは外部ネットワークアクセスが必要なため、
ネットワーク制限のあるサンドボックス環境では実行できない。
GitHub Actions(CI)上での実行を想定している。
"""

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

PROCESSED_DIR = Path(__file__).resolve().parents[2] / "data" / "processed"
RASTERS_DIR = PROCESSED_DIR / "rasters"

# 島本町周辺のバウンディングボックス (lon_min, lat_min, lon_max, lat_max)
# 島本町役場付近(北緯34.88度, 東経135.67度)を中心とした仮の範囲。
# 実データが揃ったら対象範囲に合わせて調整する。
BBOX = (135.55, 34.75, 135.85, 35.00)
# zoom14(約10m/px)だとファイルサイズが80MBを超え動作が重くなるため、
# zoom13(約20m/px)に落として軽量化する。バッファ解析(数百m単位)には十分な解像度。
ZOOM = 13
TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/dem/{z}/{x}/{y}.txt"
TILE_SIZE = 256
ELEVATION_NODATA = -32768

WEB_MERCATOR_EXTENT = 20037508.342789244  # 半周(m)


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def tile_bounds_3857(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    n = 2 ** zoom
    tile_size_m = (WEB_MERCATOR_EXTENT * 2) / n
    x_min = -WEB_MERCATOR_EXTENT + x * tile_size_m
    x_max = x_min + tile_size_m
    y_max = WEB_MERCATOR_EXTENT - y * tile_size_m
    y_min = y_max - tile_size_m
    return x_min, y_min, x_max, y_max


def fetch_tile(session: requests.Session, x: int, y: int, zoom: int) -> np.ndarray:
    url = TILE_URL.format(z=zoom, x=x, y=y)
    try:
        res = session.get(url, timeout=10)
    except requests.RequestException:
        return np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")

    if res.status_code != 200:
        # データが存在しないタイル(海上など)はNaN埋めで扱う
        return np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")

    rows = []
    for line in res.text.strip().splitlines():
        values = [np.nan if v == "e" else float(v) for v in line.split(",")]
        rows.append(values)
    arr = np.array(rows, dtype="float32")
    if arr.shape != (TILE_SIZE, TILE_SIZE):
        return np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")
    return arr


def build_mosaic() -> tuple[np.ndarray, float, float, float]:
    lon_min, lat_min, lon_max, lat_max = BBOX
    x_min_tile, y_min_tile = lonlat_to_tile(lon_min, lat_max, ZOOM)  # 左上
    x_max_tile, y_max_tile = lonlat_to_tile(lon_max, lat_min, ZOOM)  # 右下

    xs = range(x_min_tile, x_max_tile + 1)
    ys = range(y_min_tile, y_max_tile + 1)

    n_cols = len(xs)
    n_rows = len(ys)
    total = n_cols * n_rows
    mosaic = np.full((n_rows * TILE_SIZE, n_cols * TILE_SIZE), np.nan, dtype="float32")

    print(f"タイル取得中: {n_cols} x {n_rows} = {total}枚 (zoom={ZOOM})", flush=True)

    jobs = [(row_i, col_i, tx, ty) for row_i, ty in enumerate(ys) for col_i, tx in enumerate(xs)]
    session = requests.Session()
    done = 0

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(fetch_tile, session, tx, ty, ZOOM): (row_i, col_i)
            for row_i, col_i, tx, ty in jobs
        }
        for future in as_completed(futures):
            row_i, col_i = futures[future]
            tile = future.result()
            r0 = row_i * TILE_SIZE
            c0 = col_i * TILE_SIZE
            mosaic[r0:r0 + TILE_SIZE, c0:c0 + TILE_SIZE] = tile
            done += 1
            if done % 20 == 0 or done == total:
                print(f"  {done}/{total} 枚取得完了", flush=True)

    # モザイク全体のWeb Mercator範囲
    left, _, _, top = tile_bounds_3857(x_min_tile, y_min_tile, ZOOM)
    _, bottom, right, _ = tile_bounds_3857(x_max_tile, y_max_tile, ZOOM)

    pixel_size = (right - left) / (n_cols * TILE_SIZE)
    return mosaic, left, top, pixel_size


def main() -> None:
    mosaic, left, top, pixel_size = build_mosaic()

    raw_3857_path = RASTERS_DIR / "_tmp_elevation_3857.tif"
    RASTERS_DIR.mkdir(parents=True, exist_ok=True)

    transform_3857 = from_origin(left, top, pixel_size, pixel_size)
    profile = {
        "driver": "GTiff",
        "height": mosaic.shape[0],
        "width": mosaic.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:3857",
        "transform": transform_3857,
        "nodata": np.nan,
    }
    with rasterio.open(raw_3857_path, "w", **profile) as dst:
        dst.write(mosaic, 1)
    print("モザイク結合完了。EPSG:4326へ再投影中...", flush=True)

    # EPSG:4326へ再投影
    dst_path = RASTERS_DIR / "gsi_elevation.tif"
    with rasterio.open(raw_3857_path) as src:
        dst_crs = "EPSG:4326"
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        kwargs = src.meta.copy()
        kwargs.update({"crs": dst_crs, "transform": transform, "width": width, "height": height})

        tmp_reproj_path = RASTERS_DIR / "_tmp_elevation_4326.tif"
        with rasterio.open(tmp_reproj_path, "w", **kwargs) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )

    # float32 -> int16に変換して軽量化する(標高はcm単位の精度は不要なため)。
    print("int16へ変換して軽量化中...", flush=True)
    tmp_int16_path = RASTERS_DIR / "_tmp_elevation_int16.tif"
    with rasterio.open(tmp_reproj_path) as src:
        data = src.read(1)
        nodata_mask = np.isnan(data)
        data_int16 = np.round(data).astype("int16")
        data_int16[nodata_mask] = ELEVATION_NODATA

        int16_profile = src.profile.copy()
        int16_profile.update({"dtype": "int16", "nodata": ELEVATION_NODATA})
        with rasterio.open(tmp_int16_path, "w", **int16_profile) as dst:
            dst.write(data_int16, 1)

    cog_translate(
        str(tmp_int16_path),
        str(dst_path),
        cog_profiles.get("deflate"),
        config={"GDAL_TIFF_INTERNAL_MASK": "NO"},
        overview_level=0,
        in_memory=False,
        quiet=True,
    )

    raw_3857_path.unlink()
    tmp_reproj_path.unlink()
    tmp_int16_path.unlink()

    catalog_entry = {
        "id": "gsi_elevation",
        "name": "標高（国土地理院 標高タイル DEM10B）",
        "unit": "m",
        "description": "国土地理院の標高タイル(DEM10B, 10mメッシュ)から取得した島本町周辺の標高データ。",
        "path": "rasters/gsi_elevation.tif",
    }
    merge_catalog(PROCESSED_DIR / "catalog.json", [catalog_entry], "rasters")

    print(f"完了: {dst_path}")


def merge_catalog(existing_path: Path, new_entries: list, key: str) -> None:
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

    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
