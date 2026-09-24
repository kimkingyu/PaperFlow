# 学术论文去 AI 味与降低 AIGC 查重综合规约 (Anti-AI Playbook)

本规约总结了真实学术界审稿人对 AI 生成文本的敏锐特征，并提供针对性的学术句式重塑方案。

---

## 一、 中文论文高频 AI 套话查杀与改写对照表

| 常见 AI 套话 | 问题剖析 | 学术级人类重写范式 |
| :--- | :--- | :--- |
| **不可否认的是** | 空洞的口水过渡词，无信息增益 | **直接陈述事实**：“实测数据显示，...” 或直接给出论断 |
| **深入探讨 / 深入剖析** | 泛泛而谈，缺乏具体工程/数学动作 | **替换为具体动作**：“定量评估”、“形式化推导”、“实证检验” |
| **为...注入了强劲动力** | 文学抒情色彩浓厚，非客观学术语言 | **客观陈述收益**：“显著降低了推理时延”、“吞吐量提升了 28.5%” |
| **宛如 / 犹如** | 严禁在工科/理论论文中使用比喻 | **采用架构定义**：“构成...的核心拓扑链路” |
| **值得注意的是** | 机械式提示词 | **前置观察结论**：“消融实验进一步揭示出...” |
| **在当今...飞速发展的背景下** | 极其廉价的开篇假大空套话 | **开门见山陈述研究痛点**：“在边缘端部署高分辨率多模态大模型时，面临显存带宽不足的瓶颈” |
| **本研究旨在...** | 被动弱势表达 | **主动学术断言**：“本文提出并实现了一种...” |

---

## 二、 英文论文高频 AI 词汇禁忌表

| AI 高频词 | 审稿人印象 | 推荐替代词 |
| :--- | :--- | :--- |
| **delve into** | 一眼 ChatGPT 生成 | investigate, examine, analyze, scrutinize |
| **a testament to** | 俗套文学陈词 | demonstrates, indicates, serves as concrete evidence for |
| **it is worth noting that** | 凑字数无意义填充 | Notably, / We observe that / Crucially, |
| **vital / crucial role** | 空泛且泛滥 | plays an instrumental function in / provides the basis for |
| **tapestry / interwoven** | 典型的生成式幻象辞藻 | structured architecture, interdependent mechanisms |
| **fostering** | 缺乏技术动作感 | enabling, accelerating, facilitating |
| **consecutive Furthermore/Moreover** | 机械连接词三连 | 拆分句子，改用因果从句（"Owing to...", "Consequently, ..."） |

---

## 三、 人类学者写作的 4 大特征

1. **不对称性（Asymmetry）**：段落长短不一。核心机理段落详尽严密，过渡小结干净利落（1~2句）。
2. **主动语态自信感**：明确提出“We observe”, “We formulate”, “Our benchmark proves”，而非无休止的“It is believed that”.
3. **精准限定范围**：不讲绝对化的大话，加上严格的实验边界条件（如：“在batch size=32且网络抖动小于5ms的条件下”）。
4. **数据归因代替泛泛吹捧**：不用“大幅提升”，只写“平均耗时从 45.2ms 压缩至 18.6ms（降幅达 58.8%）”。
