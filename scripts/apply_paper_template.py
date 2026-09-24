"""Apply full academic paper template into active Word document via MCP tools.

Demonstrates:
- Target document locking (D:\\tes\\test.docx)
- Academic preset styling (GB/T 7713.2-2022 + GB/T 7714-2025)
- Title, Abstract, Keywords, Multi-level Headings
- Academic three-line table insertion
- Ethics, Conflict of Interest & Data Availability Statements
- Reference list formatting
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    target_file = r"D:\tes\test.docx"
    print("=" * 65)
    print("   PaperFlow · 正在通过 MCP 写入标准学术论文全套格式")
    print(f"   目标文档: {target_file}")
    print("=" * 65)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "paperflow", "run"],
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. 锁定并激活目标文档 D:\tes\test.docx
            print("\n[*] 步骤 1/8: 锁定并激活目标 Word 文档...")
            r1 = await session.call_tool("select_target_word_doc", {"file_path": target_file})
            print("   ", json.loads(r1.content[0].text)["message"])

            # 清理旧文档内容，重新生成一份纯净无重复的规范文档
            print("[*] 清理旧内容...")
            await session.call_tool("clear_word_document", {})

            # 2. 应用 2021-2026 学术排版标准预设
            print("[*] 步骤 2/8: 应用学术排版样式 (宋体/Times New Roman, 12pt, 1.5倍行距)...")
            r2 = await session.call_tool("apply_academic_style_preset", {"standard_id": "chinese_thesis_standard"})
            print("   ", json.loads(r2.content[0].text)["details"])

            # 3. 写入论文标题与中英文摘要
            print("[*] 步骤 3/8: 写入论文大标题 (heading_level=-1, 二号黑体居中, 不进入章节导航) 与中英文摘要...")
            await session.call_tool("write_to_active_word", {
                "content": "基于深度自适应特征融合的智能边缘计算模型研究",
                "heading_level": -1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "【摘 要】本文针对边缘计算设备计算资源受限与多源异构数据融合的实时性挑战，提出了一种基于动态注意力机制的轻量级自适应特征融合模型。通过在多尺度特征图上构建参数解耦的压缩投影算子，大幅削减了边缘推理过程中的浮点运算冗余。实验结果表明，在标准工业基准数据集上，本模型在保持 82.4% Top-1 识别精度的同时，将端到端延迟降低了 43.6%，显存开销削减了 37.8%，为复杂边缘感知任务的轻量化落地提供了有效解决方案。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "【关键词】边缘计算；特征融合；注意力机制；模型轻量化；自适应推理",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "ABSTRACT\nWith the pervasive deployment of Internet-of-Things (IoT) edge devices, lightweight multi-modal feature fusion under stringent computational constraints has become a crucial bottleneck. In this paper, we propose an adaptive dynamic attention fusion architecture that decouples redundant floating-point operations through compressed projection operators. Extensive experiments demonstrate that our method reduces end-to-end inference latency by 43.6% while preserving 82.4% Top-1 accuracy, providing a robust and generalizable paradigm for low-power edge intelligence.",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "Keywords: Edge Computing; Feature Fusion; Attention Mechanism; Model Compression; Adaptive Inference",
                "heading_level": 0,
                "position": "end",
            })

            # 4. 写入第 1 章 绪论
            print("[*] 步骤 4/8: 写入第 1 章 绪论（规范一级/二级标题与论述）...")
            await session.call_tool("write_to_active_word", {
                "content": "第1章 绪论",
                "heading_level": 1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "1.1 研究背景与意义",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "近年来，随着物联网感知终端的规模化部署与工业智能化转型的加速推进，边缘计算作为一种将计算与存储资源下沉至数据源头的分布式范式，受到了学术界与工业界的广泛关注 [@satyanarayanan2017, @wang2020]。在智能制造、无人驾驶与远程医疗等高实时性场景中，终端传感器产生的数据呈现出高维度、多模态以及高并发的典型特征。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "1.2 国内外研究现状",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "在模型轻量化与多尺度融合领域，现有工作主要聚焦于结构剪枝、权重量化与知识蒸馏三类技术路线 [@he2016, @vaswani2017]。然而，上述方法多依赖离线静态优化假设，在实际动态多变的边缘环境下面临泛化性能衰减与延迟波动的双重瓶颈。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "1.3 本文主要研究内容与创新贡献",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "针对上述关键技术挑战，本文的主要创新工作与学术贡献归纳如下：\n（1）提出了一种动态参数解耦的轻量级特征融合算子，有效平衡了表达能力与计算开销；\n（2）设计了基于上下文感知的时空自适应调度机制，实现了端侧资源的平滑分配；\n（3）在多类典型边缘硬件平台上完成了完备的消融实验与基准验证。",
                "heading_level": 0,
                "position": "end",
            })

            # 5. 写入第 2 章 核心方法与学术三线表
            print("[*] 步骤 5/8: 写入第 2 章 核心方法与标准学术三线表...")
            await session.call_tool("write_to_active_word", {
                "content": "第2章 理论基础与模型架构",
                "heading_level": 1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "2.1 系统模型与问题形式化",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "考虑由 N 个边缘节点与 1 个近端汇聚网关构成的异构感知系统，将输入张量序列记为 X。特征提取网络的目标是在满足最大推理延迟约束下最小化重构误差。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "2.2 核心模块超参数与配置分析",
                "heading_level": 2,
                "position": "end",
            })
            # 插入学术标准三线表
            await session.call_tool("insert_academic_table", {
                "headers": ["模块名称", "卷积核尺寸", "通道数 (In/Out)", "计算复杂度 (GFLOPs)", "量化精度"],
                "rows": [
                    ["Input Projection", "3 × 3", "3 / 64", "0.24", "FP16"],
                    ["Adaptive Fusion Block", "1 × 1 / 3 × 3", "64 / 128", "0.85", "INT8"],
                    ["Dynamic Head", "1 × 1", "128 / 10", "0.12", "INT8"],
                    ["Total / Average", "-", "-", "1.21", "Hybrid"],
                ],
                "caption": "表 2-1  核心算法模块参数配置与计算复杂度分析",
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "注：测试硬件平台采用 NVIDIA Jetson Orin Nano (8GB)，浮点算力基准测试环境为 TensorRT 8.6。如表 2-1 所示，主要计算负载集中于自适应融合模块，但整体控制在 1.21 GFLOPs 以内。",
                "heading_level": 0,
                "position": "end",
            })

            # 6. 写入第 3 章与第 4 章
            print("[*] 步骤 6/8: 写入第 3 章 实验验证与第 4 章 结论展望...")
            await session.call_tool("write_to_active_word", {
                "content": "第3章 实验验证与结果对比",
                "heading_level": 1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "3.1 实验环境与基准数据集",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "实验选取两个公开工业边缘视觉数据集与一个本地多模态采集数据集，对各类算法在吞吐率、显存占用与抗抖动能力上展开了横向评估。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "第4章 总结与未来展望",
                "heading_level": 1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "4.1 研究工作总结",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "本文设计并实现了一种面向轻量边缘计算的自适应特征融合架构，较好地兼顾了推理精度与能效比。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "4.2 未来研究方向",
                "heading_level": 2,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "未来将进一步探索引入在线自监督微调与小样本增量更新策略，以持续强化边缘模型的开放域适应能力。",
                "heading_level": 0,
                "position": "end",
            })

            # 7. 写入致谢与 GB/T 7713.2-2022 声明
            print("[*] 步骤 7/8: 写入致谢与新国标声明（利益冲突/数据可用性）...")
            await session.call_tool("write_to_active_word", {
                "content": "致谢",
                "heading_level": 1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "衷心感谢导师在论文开题、模型设计与实验推进过程中的悉心指导与严谨把关。感谢实验室同组同学在基准数据采集与硬件调试中给予的大力支持。",
                "heading_level": 0,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "利益冲突声明与数据可用性说明",
                "heading_level": 1,
                "position": "end",
            })
            await session.call_tool("write_to_active_word", {
                "content": "【利益冲突声明】作者声明本研究不存在任何商业或利益冲突。\n【数据与代码可用性】本论文实验所采用的代码实现及评估脚本已遵循开源许可协议，可在公共代码托管平台获取。",
                "heading_level": 0,
                "position": "end",
            })

            # 8. 写入参考文献（GB/T 7714-2025 规范格式）
            print("[*] 步骤 8/8: 写入参考文献清单（GB/T 7714-2025 规范）...")
            await session.call_tool("write_to_active_word", {
                "content": "参考文献",
                "heading_level": 1,
                "position": "end",
            })
            refs = [
                "[1] SATYANARAYANAN M. The emergence of edge computing[J]. Computer, 2017, 50(1): 30-39.",
                "[2] WANG X, HAN Y, LEUNG V C, et al. Convergence of edge computing and deep learning: a comprehensive survey[J]. IEEE Communications Surveys & Tutorials, 2020, 22(2): 869-904.",
                "[3] VASWANI A, SHAZEER N, PARMAR N, et al. Attention is all you need[C]//Advances in Neural Information Processing Systems. Long Beach: Curran Associates, Inc., 2017: 5998-6008.",
                "[4] HE K, ZHANG X, REN S, et al. Deep residual learning for image recognition[C]//Proceedings of the IEEE Conference on Computer Vision and Pattern Recognition. Las Vegas: IEEE, 2016: 770-778.",
                "[5] 国家市场监督管理总局, 国家标准化管理委员会. 学术论文编写规则: GB/T 7713.2-2022[S]. 北京: 中国标准出版社, 2022.",
            ]
            for ref in refs:
                await session.call_tool("write_to_active_word", {
                    "content": ref,
                    "heading_level": 0,
                    "position": "end",
                })

            # 读取写入完成后的文档信息
            final_info = await session.call_tool("get_active_word_doc", {"file_path": target_file})
            print("\n" + "=" * 65)
            print("[√] 论文全套格式与内容生成完毕！最新 Word 文档信息：")
            print(final_info.content[0].text)
            print("=" * 65)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
