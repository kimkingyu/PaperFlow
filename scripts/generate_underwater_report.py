"""Reproduce the project's report using explicit, caller-authored judgments.

This example is NOT an automatic semantic scoring algorithm or an LLM client.
The judgments below were authored for this specific research idea. Reassess when
its evidence, topic or completion level changes. No manuscript leaves this machine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from paperflow.engine.journal_finder import JournalFinder
from paperflow.engine.journals.builder import load_curated_candidates

TEXT = """研究计划：水下机械臂水动力负载解耦与端侧 NPU 实时视觉协同。
已有素材之一是公开 MATLAB 水下机械臂工具箱，包含三自由度机构建模、重力/浮力/水阻/惯性负载分解、闭式轨迹与能量账本；项目 README 报告能量残差约 7.44e-15 J，这仅说明特定仿真下的数值一致性，不等于物理参数已经实验标定。
另一个素材是 RK3588 端侧视觉与机械臂控制工程，项目 README 报告三核独立 RKNNLite 实例异步并发，从38.5提升到104.5 FPS，P99抖动下降89%；这些是项目自报的特定测试结果，不是完整水下闭环系统的验证。
拟结合水动力负载模型、分头INT8量化、NPU多核调度和视觉反馈，评估资源受限水下机器人对扰动与长尾时延的鲁棒性。拟补充水动力参数敏感性、单核/三核与量化消融、准确率/吞吐/端到端P99/能耗、不同光照浑浊度与控制周期，以及HIL半实物闭环实验。
目前不能声称已完成真实水下作业、完整HIL、硬实时截止期保证或独立硬件闭环标定。应根据主贡献拆分海洋机理路线与嵌入式实时系统路线，不把两项工程简单拼接当成创新。
"""

# Scope-fit and project-goal-fit assessments, NOT acceptance probabilities.
JUDGMENTS = {
    "IEEE Access": (91, 92, "宽口径工程系统验证适合端侧软硬件协同路线"),
    "Electronics": (94, 92, "嵌入式电子系统、加速器调度与软硬件协同方向匹配"),
    "Sensors": (88, 87, "突出视觉感知、传感融合与水下环境适应性"),
    "Journal of Marine Science and Engineering": (97, 92, "水下机械臂动力学和海洋装备验证为直接主题"),
    "Applied Sciences": (85, 84, "工程仿真与应用系统验证可形成完整应用型工作"),
    "Machines": (87, 84, "机械系统建模、驱动与控制实现需要联合验证"),
    "Actuators": (85, 82, "适合以关节执行器、驱动力矩和控制为中心的稿件"),
    "Processes": (60, 65, "过程控制只有与目标工业过程紧密联系才有优势"),
    "Robotics": (91, 85, "机器人系统方向吻合，但须独立核对所需索引"),
    "PLOS ONE": (67, 69, "方法严谨性路线可考虑，主题针对性弱于专业工程刊"),
    "PeerJ Computer Science": (82, 81, "宜突出可复现端侧计算方法而非纯水动力仿真"),
    "Microprocessors and Microsystems": (93, 85, "嵌入式微处理器与并行执行的系统性结果较契合"),
    "IEEE Embedded Systems Letters": (90, 83, "需把NPU调度贡献压缩成一个清晰可验证的短篇创新"),
    "International Journal of Advanced Robotic Systems": (91, 88, "机器人建模、视觉与控制集成具有范围适配"),
    "Journal of Systems Architecture": (98, 93, "优先突出异步调度、拷贝瓶颈与实时系统架构贡献"),
    "Journal of Real-Time Image Processing": (98, 92, "视觉精度与端到端延迟、吞吐和长尾抖动直接匹配"),
    "Mechatronics": (94, 87, "机电模型、驱动与感知闭环需要统一实验链"),
    "Applied Ocean Research": (97, 91, "适合突出水动力负载机理、参数标定与扰动分析"),
    "Computers & Electrical Engineering": (90, 87, "软硬件协同与电气控制系统路线可匹配"),
    "ACM Transactions on Embedded Computing Systems": (95, 82, "需要普适性系统方法与多负载实验而非单板优化报告"),
    "Journal of Marine Science and Application": (92, 88, "水下装备与船海工程主题契合，索引和费用待单独核实"),
    "Chinese Journal of Mechanical Engineering": (86, 80, "需突出可推广的机械系统机制及实验验证"),
    "Journal of Systems Engineering and Electronics": (83, 77, "系统控制贡献需强于常规平台集成"),
    "Measurement": (82, 77, "只有以测量误差、标定与信号质量为主贡献时优先"),
    "Ocean Engineering": (98, 81, "海洋动力学路线高度匹配，必须补充物理标定与水下验证"),
    "IEEE Journal of Oceanic Engineering": (98, 78, "主题适合，但高质量海洋工程验证缺口明显"),
    "IEEE/ASME Transactions on Mechatronics": (95, 77, "需要机理与闭环控制方法的实质创新"),
    "IEEE Transactions on Industrial Informatics": (94, 74, "工业边缘智能路线需多任务泛化与可靠性证据"),
    "IEEE Transactions on Industrial Electronics": (85, 64, "需要驱动控制方法及高质量实物实验，不宜只有部署优化"),
    "IEEE Internet of Things Journal": (75, 63, "需增加网络化水下感知或物联网系统贡献"),
    "Robotics and Autonomous Systems": (92, 77, "自主感知与闭环作业能力必须得到充分验证"),
    "Journal of Field Robotics": (92, 73, "真实非结构化环境部署是目前最大的证据缺口"),
    "Engineering Applications of Artificial Intelligence": (88, 71, "AI方法创新、强基线和跨场景泛化需要补齐"),
    "Advanced Engineering Informatics": (70, 60, "工程知识建模贡献目前不充分，不应只靠题目包装"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=".narrafork/reports/underwater_npu")
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    service = JournalFinder(str(out / "ephemeral-no-database"))
    prepared = service.prepare_manuscript(text=TEXT, mode="idea")["data"]
    profile = {
        "input_id": prepared["input_id"], "mode": "idea",
        "title": "水下机械臂仿真 + 端侧 NPU：分路线选刊与补强报告",
        "summary": "以公开水动力仿真与RK3588视觉工程为基础，计划建立动力学机理、实时计算与HIL验证链；目前尚未完成完整水下闭环。",
        "keywords": ["水下", "机器人", "嵌入式", "实时", "海洋", "robotics", "NPU", "embedded"],
        "article_type": "research", "readiness": "planned", "data_scale": "small_sample",
        "data_openness": "experimental_sample", "model_paradigm": "deep_learning",
    }
    records = [r.model_dump(mode="json") for r in load_curated_candidates() if r.title in JUDGMENTS]
    preferences = {"goal": "efficiency", "per_group": 7, "candidate_limit": 100}
    packet = {"text": TEXT, "mode": "idea", "profile": profile, "candidate_records": records,
              "preferences": preferences}
    first = service.recommend(**packet)
    assessments = []
    for item in first["data"]["assessment_targets"]:
        scope, goal, rationale = JUDGMENTS[item["title"]]
        entry = next(r for r in records if r["title"] == item["title"])
        quote = entry["editorial_profiles"][0]["scope_summary"][:100]
        assessments.append({
            "journal_id": item["journal_id"], "context_id": first["data"]["context_id"],
            "scope_fit": scope, "goal_fit": goal,
            "evidence": [{"manuscript_quote": "水下机械臂水动力负载解耦与端侧 NPU 实时视觉协同", "journal_quote": quote}],
            "rationale": rationale + "；本分数评估主题与当前研究计划的适配，不预测录用率。",
            "gaps": ["补齐物理参数标定与独立验证，数值守恒不是现实正确性的证明",
                     "报告量化精度损失、端到端P99/截止期违约率、能耗及多核消融",
                     "完成HIL或水下闭环后再判断完成度；投稿前核对当前索引、预警与学院规则"],
        })
    packet["assessments"] = assessments
    result = service.recommend(**packet, html_path=str(out / "underwater_npu_report.html"), overwrite_html=True)
    for name, data in (("recommendation.json", result), ("request_packet.json", packet)):
        (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "research_idea.txt").write_text(TEXT, encoding="utf-8")
    print(json.dumps({"html_path": result["data"]["html_path"], "stage": result["data"]["stage"],
                      **result["data"]["display_summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
