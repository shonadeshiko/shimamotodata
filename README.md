# 京都・大阪 GIS解析プラットフォーム（shimamotodata）

京都府・大阪府の広域を対象に、空間演算・ラスタ解析・ポテンシャル測定を行い、
結果を静的サイトとして公開するためのリポジトリです（当初は大阪府島本町単体の
プロトタイプとして開始し、京都・大阪広域データの取り込みに合わせて対象範囲を拡大）。

姉妹リポジトリ [gisdata](https://github.com/shonadeshiko/gisdata)（千葉県印旛沼流域版）の
コード構成をコピーし、地域名を置き換えて作成。

## 設計方針

- **原則として静的配信**。サーバーを常時起動させず、コストを最小化する
- **重い処理（ラスタ解析・流域解析など）は事前バッチで計算**し、結果を
  Cloud Optimized GeoTIFF（COG）として書き出す
- **軽い計算（バッファ集計など）はブラウザ内**（turf.js + geotiff.js）で
  オンデマンド実行する
- データが継続的に増えても、パイプラインを再実行するだけで
  成果物を再生成できるようにする（メンテナンス性）

## 構成

```
shimamotodata/
├── pipeline/
│   ├── process/
│   │   ├── ingest_shimamoto_data.py   # 京都・大阪広域の実データ取り込み
│   │   └── ingest_gsi_elevation.py    # 国土地理院 標高タイル取得
│   └── requirements.txt
├── data/
│   ├── raw/shimamoto/{raster,vector}/ # 元データの置き場（.gitignore対象）
│   └── processed/                     # 生成物（COG, GeoJSON）
├── web/
│   └── index.html                     # フロントエンド（MapLibre GL + turf.js + geotiff.js）
├── .github/workflows/
│   ├── pipeline.yml                   # ビルド・GitHub Pages公開
│   └── ingest-elevation.yml           # 標高データ取得（手動実行）
└── README.md
```

## セットアップ（ローカルでパイプラインを試す）

```bash
cd pipeline
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python process/ingest_shimamoto_data.py   # 元データが data/raw/shimamoto/ にある場合
```

新しいデータを追加したい場合は `pipeline/process/ingest_shimamoto_data.py` の
`RASTER_DEFS` / `VECTOR_DEFS` に定義を1つ追加するだけでよい。連続値のデータを
uint8で軽量化したい場合は `uint8_scale` を指定する（例: 10を指定すると
値を10倍してuint8(0-255)に丸めて保存し、frontend側で10で割り戻す）。

## フロントエンドの確認

```bash
mkdir -p web/data
cp -r data/processed/* web/data/
cd web
python -m http.server 8000
```

（GitHub Actionsでは、この処理が自動的に行われる）

## ベースマップについて

デフォルトは国土地理院の淡色地図（`gsi_pale`）。`web/index.html` 内の
`BASE_LAYERS` 配列にエントリを1つ追加すると、画面上部のセレクタに
選択肢が増える仕組みになっている。

「旧版地形図」は、近畿地方(keihansin)の今昔マップ・2万分の1版
（`https://ktgis.net/kjmapw/kjtilemap/keihansin/2man/{z}/{x}/{y}.png`）
を使用（TMS方式のため `scheme: "tms"` を指定）。

## メッシュの色分けについて

`web/index.html` の `MESH_SCORES` は、gisdata側の500mメッシュ
(GI保全スコア・GI開発圧スコア・優先度ランク・市街地率・森林率)の
属性名を踏襲しているが、京都・大阪広域の実メッシュデータ
(`メッシュ500m_GI統合v2_京都大阪.gpkg`、23,673メッシュ)も同じ属性名
(`gi_conservation_score`/`gi_pressure_score`/`priority_rank`/
`urban_frac`/`forest_frac`等)で作られていることを確認済み。

## 標高データ（国土地理院 標高タイル）について

`pipeline/process/ingest_gsi_elevation.py` は、国土地理院の標高タイル
(DEM10B, 10mメッシュ)を島本町周辺の範囲だけダウンロードし、
モザイク結合・EPSG:4326への再投影・COG化を行う。BBOXは島本町役場
付近を中心とした仮の範囲になっているので、対象範囲が決まったら
スクリプト内の `BBOX` を調整すること。

外部ネットワークへの多数アクセスが必要で時間もかかるため、通常の
pushでは実行せず、GitHub Actionsの `Ingest GSI Elevation Data`
ワークフロー（手動実行 = workflow_dispatch）でのみ実行する。
実行結果（COGファイルとカタログ更新）はリポジトリにコミットされ、
以後は固定データとして配信される。

## バッファ解析について

地図をクリックすると、その地点を中心とした指定半径のバッファ内で、
カタログに登録されている全ラスタの平均・最小・最大値をブラウザ内
（geotiff.js + turf.js）でオンデマンド計算する。サーバー計算は不要。
一度取得したラスタはブラウザ内にキャッシュされ、2回目以降のクリックで
再取得しないようになっている。

## 京都・大阪 実データについて

`pipeline/process/ingest_shimamoto_data.py` は、京都・大阪広域の実データ
（水田占有率・HANDランク・開発圧(2011-2022/2020-2024)・TWIランク・
GI地形スコアのラスタ、京都・大阪域(N03行政界)・500mメッシュGI統合v2
のベクタ）を取り込み、EPSG:4326への再投影・COG化・GeoJSON化を行う。

元データは `data/raw/shimamoto/{raster,vector}/` に配置する想定（Git管理外）。

`RASTER_DEFS` の `"src"` にファイル名のリストを渡すと、複数ファイル
（例: 京都府・大阪府に分かれたデータ）をモザイク結合してから処理する
（`merge_rasters()` 参照）。水田の占有率はこの仕組みで
`05_水田の占有率_26_京都府.tif` + `05_水田の占有率_27_大阪府.tif` を
結合している。

### データの注意点

- **ファイルサイズ**：対象範囲が島本町単体から京都・大阪広域に拡大した
  ことで、各ラスタが数MB〜20MB弱、メッシュGeoJSONが約26MBまで増加した。
  本サイトは「ブラウザが全ファイルを丸ごとfetchする」設計のため、
  初回読み込みが重くなる。今後データが増える場合は、メッシュの
  ジオメトリ簡略化やタイル化(PMTiles等)を検討する。
- **未取り込みのデータ種類**：今回アップロードされた以下のデータは
  まだ `RASTER_DEFS`/`VECTOR_DEFS` に未登録。取り込む場合は
  カテゴリカルな値の色分け(凡例)など、フロントエンド側の追加実装が
  必要になる見込み。
  - `04_自然的景観の多様度_26_京都府.tif` / `_27_大阪府.tif`（要モザイク結合）
  - `JAXA_HRLC土地被覆_2020_京都大阪.tif` / `_2024_京都大阪.tif`（カテゴリカル）
  - `03_地形・地質等から期待される雨水浸透機能_26_京都府.shp` /
    `_27_大阪府.shp`（シェープファイル、要マージ）

## 今後のTODO

- [ ] メッシュ・境界データのサイズ最適化（ジオメトリ簡略化 / タイル化）
- [ ] 自然的景観の多様度・JAXA土地被覆・雨水浸透機能データの取り込み
- [ ] `ingest_gsi_elevation.py` の `BBOX` を対象範囲に合わせて調整
- [ ] Cloudflare R2 / Pages への接続とデプロイ設定
