# Xeltri PCB 分析輸出（唯讀）

此目錄由 `scripts/` 下的分析工具產生，**不是** KiCad 設計源檔。

## 使用方式

### 方式 A：在 Cursor 對話（推薦）

1. KiCad **Ctrl+S 存檔**
2. 在對話貼座標 + 問題，例如：

> 已存檔。區域 (158,65) 到 (168,75)。這裡 GND via 夠嗎？

Agent 會自動跑 `analyze_pcb_region.py` 並回覆。

**座標規則：**
- **2 點** → 對角線形成的矩形
- **3 點以上** → 多邊形邊界

### 方式 B：手動命令

```powershell
cd E:\Xeltri

# 一鍵：匯出整板 + 區域查詢
python scripts/analyze_pcb_region.py --region 158,65,168,75
python scripts/analyze_pcb_region.py --region 140,68,150,68,150,75,140,75 --layers F.Cu,In2.Cu

# 或分開執行
python scripts/export_board_model.py
python scripts/query_region.py --rect 158,65,168,75
```

輸出指標：`analysis/latest_region.json`、`analysis/latest_session.json`

## 輸出檔

| 檔案 | 說明 |
|------|------|
| `board_model.json` | 整板 stackup、元件、net 走線統計、zone 索引 |
| `zones_geometry.json` | **全部铜箔 zone** 轮廓 + fill 后多边形（完整几何） |
| `latest_region.json` | 最近一次區域查詢（含 zone 覆盖率） |
| `latest_zones_geometry.json` | 指向最新 zones 几何 |

## 依賴

```powershell
pip install -r scripts/requirements-analysis.txt
```

## 功能

- **精确** zone 覆盖率（Shapely 多边形相交面积）
- **Keepout** + footprint 内 zone
- **回流路径 / 参考平面连续性**（`return_path_checks`）
- **解析缓存** `analysis/.cache/`（第二次查询通常 <2s）

## AI 分析

Cursor Agent 讀取此目錄的 JSON 回答問題，**不修改** `KiCad/` 下的源檔（見 `.cursor/rules/kicad-readonly.mdc`）。
