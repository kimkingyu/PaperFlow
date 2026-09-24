# PaperFlow · AI 客户端与 Office/Word 实时连接桥梁 & 论文从0到1写作套件

<p align="center">
  <b>让 Tencent WorkBuddy、通义千问 Qwen、Cursor、Claude 直接“挂载连接”桌面上运行的 Microsoft Word 与 WPS！</b><br>
  涵盖实时双向控制、Word 原生批注气泡、Zotero 动态活引用、学术去 AI 味与全生命周期 0 到 1 写作流水线。
</p>

<p align="center">
  <a href="#核心亮点">核心亮点</a> •
  <a href="#一键全自动连接">一键全自动连接</a> •
  <a href="#论文从0到1工作流">从0到1工作流</a> •
  <a href="#mcp-工具清单">MCP 工具一览</a> •
  <a href="#快速开始">快速开始</a>
</p>

---

## 痛点与破局

| 传统 AI 论文辅助方式 | **PaperFlow-MCP 实时连接方案** |
| :--- | :--- |
| **严重割裂**：网页生成一堆文本，手动复制粘贴到 Word，排版全乱 | **实时互通**：AI 隔空直连桌面上打开的 Word/WPS，像打字机一样流式写入 |
| **盲人摸象**：AI 看不到你的 Word，改动某段必须整段复制发给 AI | **划选感知**：在 Word 里鼠标选中哪一段，AI 就能自动读取哪一段 |
| **虚假文本**：参考文献全是写死的纯文本，顺序一变全篇重排 | **Zotero 活引用**：原生注入 CSL 域代码，Word 里打开按一键自动刷新文献表 |
| **粗暴覆盖**：AI 直接把你的原文改得面目全非，根本不知道改了哪 | **原生批注与修订**：直接在 Word 界面右侧弹出批注气泡（Comments），支持红绿修订对照 |
| **一眼假 AI 味**：充斥着 `delve into`, `深入探讨`, `不可否认的是` | **学术去 AI 味引擎**：基于顶级学者习惯重构语态、打破对称句式、消除公式化套话 |

---

## 核心亮点

### 1. 🔌 零门槛全客户端一键自动连接 (Auto-Connect Setup)
彻底告别繁琐翻找配置文件和手动修改 JSON！
运行 `python -m paperflow setup`，脚本自动智能扫描并写入以下客户端配置：
*  **Tencent WorkBuddy / CodeBuddy**
*  **通义千问桌面端 / Qwen Agent / Qoder**
*  **Claude Desktop**
*  **Cursor**
*  **Cherry Studio**
*  **VS Code (Cline / Roo Code)**

### 2. 📝 真正的 Office/Word 实时双向交互
* **划选即感知**：用户在 Word 里用鼠标划选文字，AI 即可通过 `get_word_selection` 实时提取，省去复制粘贴。
* **原生批注气泡（Comments）**：AI 审稿意见不是吐在聊天框里，而是像导师审稿一样，直接在 Word 正文右侧生成批注气泡。
* **无缝修订模式（Track Changes）**：一键开启 Word 修订，AI 的所有润色与改写以删除线/下划线呈现，任你逐条点“接受修改”。
* **常用学术排版一键应用**：一键设置宋体/Times New Roman、12pt（小四）、1.5 倍行距、首行缩进 2 字符。这是国内学位论文的常见惯例值，**不是国家标准规定**；有学校/期刊官方模板时请以官方模板为准。

### 3. 📚 Zotero 动态活引用注入 (Live CSL Field Codes)
采用底层 OOXML 域代码构造技术，直接向 Word 注入：
* `ADDIN ZOTERO_ITEM CSL_CITATION`：动态上标引用
* `ADDIN ZOTERO_BIBL`：末尾自适应参考文献列表
在 Word 中打开后，被官方 Zotero 插件无缝识别为**活动引用**，点击插件 “Refresh” 即可自由切换 IEEE、GB/T 7714、APA 等格式！

### 4. 🛡️ 专杀 AI 味与学术降重引擎 (Anti-AI Engine)
针对主流 AIGC 检测器（Turnitin、知网、CopyLeaks 等）的特征，执行系统性重塑：
* **偏好主动学术动词**：以自信的学术断言替代弱势被动句。
* **打破句式机械对称**：引入长短句自然起伏，杜绝并列三连排比。
* **查杀禁忌套话库**：自动标记并清除中英文 18 类高频死板词汇。

### 5. 🎯 全流程选择题导引交互（告别迷茫填空）
写论文面临海量琐碎规范，若让用户做“开放式填空”会产生极重的心智负担。PaperFlow 确立了**“全流程选择题交互哲学”**：
* **开工前三问选项制**：学位/期刊类型、模板依据、GB/T 7714 2015/2025 规范版本均预置清晰选项与 `(推荐)` 标记。
* **修改呈现按需选**：无论是润色还是降重，用户可按选项一键指定“开修订模式留红绿划线”、“仅插批注气泡保留原文”或“直接无痕替换”。
* **跨平台兼容**：在 NarraFork 原生环境下呼出 UI 单选卡片；在 MCP 客户端中调用表单 Elicitation；在通用对话客户端中以带序号的选择清单呈现，用户回个数字即可。

### 6. 📐 内置 2021-2026 全系列学术标准与三线表引擎
* **国标全覆盖**：GB/T 7714-2025（最新参考文献国标）、GB/T 7714-2015（现行通用）、GB/T 7713.2-2022（学术论文编写必备12要素）、GB/T 7713.1-2006（学位论文框架）。
* **国际顶会/顶刊规范**：IEEE Transactions & Conferences (2024-2026 最新双栏)、ACM Master Template (2024-2026)、APA 7th Edition (著者-出版年制)、Nature Portfolio (2023-2026 CRediT 与数据可用性声明)。
* **学术三线表自动生成**：自动构造顶底粗线 (1.5pt)、栏目细线 (0.75pt)、无坚线、表题居中置上的标准学术表格，支持 Word 实时写入与离线 docx 编译。

---

## 论文从 0 到 1 写作全生命周期工作流

```text
 ┌─────────────────────────────────────────────────────────────┐
 │ 阶段 1: 核心故事线与多视角大纲 (STORM 启发式多专家碰撞)      │
 │  - 模拟 3 位严苛审稿人视角，锁定核心研究问题 (RQ) 与 3 点贡献  │
 │  - AI 直接将三级学术大纲注入你桌面上新建的 Word 窗口          │
 └──────────────────────────────┬──────────────────────────────┘
                                │
                                ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 阶段 2: 文献池对齐与真实证据打分 (PaperQA 风格零幻觉准则)     │
 │  - 强制 1-10 分证据相关度过滤，无确凿依据坚决不捏造文献       │
 │  - 段落末尾标注 [@KEY]，由引擎编译为 Zotero 动态域代码        │
 └──────────────────────────────┬──────────────────────────────┘
                                │
                                ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 阶段 3: 科学倒序写作法 (Golden Drafting Order)              │
 │  - 第一步：核心方法与数学形式化 (Methodology)                │
 │  - 第二步：实验评测与数据归因 (Experiments & Evaluation)     │
 │  - 第三步：引言与相关工作 (Introduction & Related Work)       │
 │  - 第四步：摘要与结论收敛 (Abstract & Conclusion)            │
 └──────────────────────────────┬──────────────────────────────┘
                                │
                                ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 阶段 4: 去 AI 味精修与导师级 Word 原生批注交互              │
 │  - 开启 Word 修订模式，直观查看改动对比                      │
 │  - 在存疑段落直接挂上 Word 批注气泡，助你逐条修改直到定稿    │
 └─────────────────────────────────────────────────────────────┘
```

---

## MCP 工具清单

| 工具名称 (Tool Name) | 核心功能说明 |
| :--- | :--- |
| `get_active_word_doc` | 读取当前桌面上打开的 Word/WPS 文档名称、路径、段落数、全文文字摘要 |
| `get_word_selection` | **实时读取**用户当前在 Word 里用鼠标高亮选中的文字段落 |
| `write_to_active_word` | 在当前打开的 Word 光标处或文末流式写入正文或标题（支持1/2/3级标题） |
| `replace_word_selection` | 将 Word 中选中的文字原地替换为润色/改写后的学术内容 |
| `insert_academic_table` | 在光标处插入**标准学术三线表**（顶底粗、栏目细、无竖线，表题在上居中） |
| `list_academic_standards` | 列出内置的 **2021-2026 年国内外全部学术标准**、特征与推荐选项 |
| `add_word_comment` | 在当前选中的文字上添加 **Word 原生审阅批注气泡（Comments）** |
| `set_word_track_revisions` | 开启或关闭 Word **修订模式（Track Changes）** |
| `apply_academic_style_preset` | 一键应用指定的 2021-2026 学术标准样式预设（没有官方模板时使用） |
| `scan_anti_ai_flavor` | 深度扫描学术文本中的 AI 套话痕迹并给出针对性降重与改写建议 |
| `generate_offline_paper_docx` | **离线模式**：基于 Markdown 结构直接生成带三线表和 Zotero 活引用的标准论文 `.docx` |

---

## 快速开始

### 1. 安装项目环境
```bash
git clone https://github.com/your-username/paperflow-mcp.git
cd paperflow-mcp

# 推荐在虚拟环境中安装
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. 一键配置你的 AI 客户端（极简！）
只需在终端敲入：
```bash
python -m paperflow setup
```
程序会自动检测你电脑上的 **WorkBuddy / 通义千问 / Cursor / Claude Desktop / Cherry Studio**，并自动写入连接配置。

### 3. 环境检测与实战连接
```bash
python -m paperflow doctor
```
打开你的 Microsoft Word 或 WPS，重启 AI 客户端，在对话框中直接对 AI 说：
> **“连接当前 Word 论文，帮我梳理三级大纲并写入文档”**

即可开始沉浸式写作！

---

## 开源协议

本项目采用 [MIT 许可证](LICENSE)。
欢迎提交 Issue 和 Pull Request，一起打造最懂学术写作的开源 AI 助手！
