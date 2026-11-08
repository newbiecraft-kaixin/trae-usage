# TRAE 积分消耗 · 桌面版

PySide6 独立窗口程序：选择日期区间，点一次「开始抓取」只跑一轮，抓完自动停止并落盘。
不循环、不轮询，也不会打开浏览器标签页。

- 深色界面：大号消耗数字、按天柱状图（悬停显示数值）、每日明细表、可折叠运行日志、状态灯
- 单次抓取：默认「今天」，支持任意起止日期；完成后按钮变为「重新抓取」
- 数据落盘：`trae_usage_data/usage_<起>_<止>.json` / `.csv`

## 项目结构

```text
trae_usage.py        # 抓取核心（Playwright + TRAE 用量接口）
trae_usage_app/      # PySide6 桌面界面
trae_usage_data/     # 运行产物与登录态（已 gitignore，不入库）
```

## 安装

```powershell
python -m pip install PySide6 playwright
python -m playwright install chromium
```

## 启动

双击桌面快捷方式 `TRAE积分消耗.lnk`；
或：

```powershell
python trae_usage_app/app.py          # 调试运行
pythonw trae_usage_app/app.py         # 无控制台
```

首次运行如果未登录：勾选「显示浏览器窗口（登录时勾选）」再抓取，在弹出的窗口里登录一次 TRAE，
登录态保存在 `trae_usage_data/profile/`，之后保持无头运行即可。

命令行跑一轮（用于排查）：

```powershell
python trae_usage_app/app.py --cli --start 2026-11-08 --end 2026-11-08
python trae_usage_app/app.py --cli --start 2026-11-07 --end 2026-11-09 --show-browser
```

## 抓取原理

1. Playwright 用持久化登录态打开 `https://www.trae.cn/dashboard#usage`，
   监听页面真实请求，取出 `authorization: Cloud-IDE-JWT ...` 凭证。
2. 直接按天请求接口
   `POST https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session`，
   body 为 `{"start_time": 当天0点, "end_time": 当天23:59:59, "page_size": 50, "page_num": N, "usage_type": [7]}`。
3. 每天独立请求，单页 50 条，自动翻页直到取满 `total`；**每次请求间隔 2 秒**，每天只抓取所选日期，不会混入其它日期。
4. 按 `usage_time`（+08 时区）再次过滤归属日期，汇总 `credits_float`（积分）与 `cost_money_float`（金额）。
5. 本轮结束即关闭浏览器上下文，不会再有请求。

## 指标口径

- **积分**：官网「用量管理 → 用量明细 → 积分消耗」列，即接口的 `credits_float`（同 `amount_float`），
  本工具按会话求和得到每天/区间的消耗，与官网逐条明细同一口径（官网没有每日汇总图表）。
- **消费金额**：接口的 `cost_money_float`，约等于 `积分 × 0.025` 的折算值，官网用量页不展示。

## 数据位置（均在 `trae_usage_data/`）

- `usage_<start>_<end>.json` / `.csv`：本次范围的每日结果（界面「打开数据文件夹」）
- `raw/requests_<时间>.jsonl`：每次请求的 URL、method、post_data、HTTP 状态、total，便于排查
- `run.log`：运行日志（不含 cookie / token）
- `profile/`：浏览器登录态，切勿外传或提交到 git

## 已知限制

- 接口 `page_size` 上限为 50（更大返回 400），单日会话多时会分页，因此一天可能发 2~3 次请求（仍保持 2 秒间隔）。
- 登录凭证（JWT）有有效期，过期会自动刷新页面重取一次；若仍失败请勾选「显示浏览器窗口」重新登录。
- 当日数据仍在增长（会话未结束），同一天不同时间抓取结果会有小幅差异。
- 仅适配 trae.cn 当前接口；接口变更需同步调整 `app.py` 中的 `ENDPOINT` 与字段名。
