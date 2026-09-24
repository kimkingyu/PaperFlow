# 基于 STORM 方法论的多视角大纲设计指南

STORM（Synthesis of Topic Outlines through Repeated Multi-perspective Questioning）是斯坦福大学提出的深度学术长文大纲编排范式。

---

## 核心流程：三大专家角色碰撞法

在下笔写大纲前，助手必须模拟 **3 位背景迥异的严苛审稿人** 对研究课题进行交叉审问：

1. **审稿人 A（理论与算法专家）**：
   - 关注：“数学形式化是否严谨？公式定义是否自洽？计算复杂度与收敛性证明在哪里？”
   - 对应大纲章节：第 3 章 系统建模与算法形式化定义（Notation & Formulation）。
2. **审稿人 B（工程系统与落地专家）**：
   - 关注：“在真实高并发/受限设备上如何部署？瓶颈是显存还是通信？异常边界条件如何处理？”
   - 对应大纲章节：第 4 章 系统工程实现与优化策略（System Implementation）。
3. **审稿人 C（批判性评测与对比专家）**：
   - 关注：“对比基准（Baselines）是否具有公信力？是真正超越了还是调参带来的偶发收益？消融实验能否证明各个模块各自的必要性？”
   - 对应大纲章节：第 5 章 实验验证与消融分析（Experiments & Ablation）。

---

## 标准三级大纲规范模板

```text
1 引言 (Introduction)
   1.1 研究背景与工程挑战
   1.2 现有方法的局限性与研究缺口 (Research Gap)
   1.3 本文的核心贡献与论文结构 (3 Contributions)
2 相关工作 (Related Work)
   2.1 [技术路线A] 的演进与瓶颈
   2.2 [技术路线B] 的最新进展
   2.3 本文工作与已有方案的本质差异对比
3 核心方法与架构设计 (Methodology)
   3.1 问题建模与数学符号定义 (Formulation & Notation)
   3.2 [核心模块1] 设计与工作机理
   3.3 [核心模块2] 动态优化算法
   3.4 复杂度与收敛性分析
4 实验评估与结果分析 (Experiments & Evaluation)
   4.1 实验环境、数据集与评测指标
   4.2 与 SOTA 基准方案的综合性能对比 (Baseline Comparison)
   4.3 消融实验与各组件有效性验证 (Ablation Study)
   4.4 敏感度分析与极端边界负载测试
5 总结与未来展望 (Conclusion & Future Work)
   5.1 全文工作总结与学术价值
   5.2 当前方案的局限性与后续研究方向
```
