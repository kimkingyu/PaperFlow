# 格式模板与样式来源清单（GitHub 调研结果）

调研日期：2026-09-24。许可证和活跃度来自 GitHub API。
**判断标准**：没有声明许可证的仓库，默认保留全部版权，**不能打包进本项目再分发**，只能给链接让用户自己去下载。

## 1. 参考文献样式（CSL）

| 仓库 | 许可证 | 活跃度 | 用途 | 能否打包 |
| :--- | :--- | :--- | :--- | :--- |
| [zotero-chinese/styles](https://github.com/zotero-chinese/styles) | CC BY-SA 3.0 | 约 6.3k star，2026-08 仍在更新 | GB/T 7714-1987/2005/2015/2025 各变体及大量高校、期刊样式 | 可以，但必须署名并按 CC BY-SA 3.0 共享；**推荐只给链接**，让用户从 https://zotero-chinese.com/styles/ 安装，版本也最新 |
| [TomBener/quarto-chinese](https://github.com/TomBener/quarto-chinese) | MIT | 2026-09 仍在更新 | 含 `gb-numeric.csl`、`gb-author-date.csl`，以及中文学术写作的 Quarto/Pandoc 流程 | 仓库是 MIT，但 CSL 文件可能另有来源协议，打包前需逐个核对文件头 |

## 2. Word 模板（docx / dotx）

| 仓库 | 许可证 | 活跃度 | 说明 | 能否打包 |
| :--- | :--- | :--- | :--- | :--- |
| [aofenghanyue/PhDThesisWordTemplate](https://github.com/aofenghanyue/PhDThesisWordTemplate) | MIT | 2021 年后未更新 | 北航博士学位论文模板（依据 2020.07 规定） | 可以（保留 MIT 声明），但只适用于北航 |
| [TomBener/pandoc-docx](https://github.com/TomBener/pandoc-docx) | GPL-2.0 | 2026-03 有更新 | Pandoc 用的 `reference.docx`（已解压为 XML） | **不建议**：GPL 与本项目 MIT 不兼容；且样式是通用英文排版（无中文字体、无首行缩进），对中文论文用处不大 |
| [liuweifly/hust-thesis-word](https://github.com/liuweifly/hust-thesis-word) | **无** | 2016 年后未更新，133 star | 华科硕士论文模板 | 不可以，只能给链接 |
| [mescoda/heu-thesis-word-template](https://github.com/mescoda/heu-thesis-word-template) | **无** | 2013 年后未更新 | 哈工程本科模板，作者对"样式化排版"思路写得很好 | 不可以，只能给链接 |
| [Eliasthunderdog/ustcthesis_word](https://github.com/Eliasthunderdog/ustcthesis_word) | 未核实 | — | 中科大本科模板 | 未核实前不打包 |
| [KingwithQueen/GXU-Masters-and-PhD-Thesis-Templates](https://github.com/KingwithQueen/GXU-Masters-and-PhD-Thesis-Templates) | 未核实 | — | 广西大学硕博模板 | 未核实前不打包 |

## 3. 标准原文

| 标准 | 链接 |
| :--- | :--- |
| GB/T 7714-2025 官方条目 | https://std.samr.gov.cn/gb/search/gbDetailed?id=4507EFE13D37CB6AE06397BE0A0A601F |
| GB/T 7714-2015 官方条目 | https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D8055ED3A7E05397BE0A0AB82A |
| GB/T 7713.1-2006 学位论文编写规则（第三方仓库存档的 PDF） | https://github.com/saccohuo/GBT-Standard |

## 4. 结论与本项目的做法

1. **GitHub 上没有一个"通用、许可证宽松、持续维护"的中文学位论文 Word 模板。** 现有的几乎都是某一所学校的，并且不少没有许可证。
2. 因此 PaperFlow **不打包第三方 Word 模板**，改为：
   - 默认：由 `paperflow/engine/docx_builder.py` 用代码生成样式（本项目自己的代码，MIT），取值见 `format_thesis_layout.md` 第 4 节。
   - 推荐：让用户提供**自己学校/期刊的官方模板**，PaperFlow 直接在该模板上写入内容，保留其样式。
3. 参考文献样式不自己实现，统一交给 Zotero + zotero-chinese 的 CSL。
