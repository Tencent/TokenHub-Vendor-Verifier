#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
效果评测报告生成器 v2（对齐效果测试报告模版.html的 6 章结构）。

数据来源（evalscope 产出目录）：
  reviews/<model>/*.jsonl      每行一题（含 messages / sample_score / perf_metrics）
  predictions/<model>/skipped_samples.jsonl   跳过样本（含 error / partial_trajectory）
  reports/<model>/*.json       evalscope 自带得分报告（score / metrics / subsets）
  configs/task_config.yaml     任务配置（模型 / generation_config / judge）

用法：
  python3 gen_eval_report_v2.py <run_dir> [-o report.html] [-m model] [--compact]
"""

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# 单条消息截断长度；紧凑模式会调小
MAX_TEXT_CHARS = 4000
MAX_FLOW_MESSAGES = 0  # 0=不限；紧凑模式限制每题渲染的消息条数

# 模版样式与脚本（由 build 步骤从模版 HTML 精确注入）
CSS = '\n* { box-sizing: border-box; }\nbody { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;\n  margin: 0; padding: 24px 32px 80px; background: #f8fafc; color: #0f172a; font-size: 14px; line-height: 1.55; }\nh1 { font-size: 24px; margin: 0 0 8px; }\nh2 { font-size: 18px; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 2px solid #e2e8f0; }\nh3 { font-size: 15px; margin: 18px 0 8px; }\ncode, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }\n.meta { color: #64748b; font-size: 13px; margin-bottom: 12px; }\n.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0; }\n.stat-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; }\n.stat-card .lbl { color: #64748b; font-size: 12px; }\n.stat-card .val { font-size: 20px; font-weight: 600; margin-top: 4px; }\n.stat-card.ok .val { color: #16a34a; } .stat-card.warn .val { color: #dc2626; }\n.metric-table { width: 100%; border-collapse: collapse; background: #fff;\n  border: 1px solid #cfe8f7; border-radius: 8px; overflow: hidden; margin: 8px 0 24px; }\n.metric-table thead tr th { background: #e6f4fb; font-weight: 600; text-align: center;\n  border: 1px solid #cfe8f7; padding: 10px 6px; font-size: 12px; white-space: nowrap; }\n.metric-table thead tr:first-child th { background: #d6ecf6; font-size: 14px; }\n.metric-table tbody td { text-align: center; padding: 10px 6px; font-variant-numeric: tabular-nums;\n  font-size: 14px; font-weight: 600; border: 1px solid #e2e8f0; white-space: nowrap; }\n.metric-table tbody td.warn { color: #dc2626; }\n.metric-table tbody td.metric-name { text-align: left; font-weight: 500; color: #334155; }\n.metric-meta { color: #475569; font-size: 15px; margin-bottom: 10px; }\n.metric-meta b { font-size: 16px; }\n.issue-summary { width: 100%; border-collapse: collapse; background: #fff; margin: 8px 0 24px;\n  border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }\n.issue-summary th { background: #f1f5f9; color: #475569; font-weight: 600; padding: 10px 8px;\n  font-size: 12px; border-bottom: 1px solid #e2e8f0; white-space: nowrap; }\n.issue-summary th.num, .issue-summary td.num { text-align: right; font-variant-numeric: tabular-nums; }\n.issue-summary td { padding: 9px 8px; border-bottom: 1px solid #f1f5f9; font-size: 13px; }\n.issue-row { cursor: pointer; } .issue-row:hover { background: #f8fafc; }\n.issue-row .caret { display: inline-block; width: 14px; color: #64748b; transition: transform .15s; }\n.issue-row.open .caret { transform: rotate(90deg); }\n.issue-detail { display: none; background: #fafbfc; } .issue-detail.show { display: table-row; }\n.issue-detail td { padding: 12px; }\n.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }\n.badge.pass { background: #dcfce7; color: #16a34a; }\n.badge.fail { background: #fee2e2; color: #dc2626; }\n.badge.score { background: #e0e9ff; color: #175cd3; font-variant-numeric: tabular-nums; }\n.badge.unknown { background: #f1f5f9; color: #64748b; }\n.badge.skip { background: #fef3c7; color: #92400e; }\n.task-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: 14px; overflow: hidden; }\n.task-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 12px 16px; cursor: pointer; }\n.task-head:hover { background: #f8fafc; }\n.task-head .caret { display: inline-block; width: 14px; color: #64748b; transition: transform .15s; }\n.task-card.open .task-head .caret { transform: rotate(90deg); }\n.task-title { font-family: ui-monospace, Menlo, monospace; font-size: 13px; font-weight: 600; }\n.task-stats { margin-left: auto; display: flex; gap: 14px; font-size: 12px; color: #64748b; }\n.task-stats b { color: #0f172a; }\n.task-body { display: none; padding: 0 16px 16px; border-top: 1px solid #f1f5f9; }\n.task-card.open .task-body { display: block; }\n.task-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 6px 18px; margin: 12px 0; font-size: 12px; }\n.task-meta .k { color: #64748b; }\n.task-meta .v { font-family: ui-monospace, Menlo, monospace; }\n/* 思维链流水 */\n.msg { margin: 8px 0; border-radius: 8px; }\n.msg pre { margin: 6px 0 0; padding: 10px 12px; border-radius: 6px; font-size: 12px;\n  white-space: pre-wrap; word-break: break-word; max-height: 420px; overflow: auto;\n  font-family: ui-monospace, Menlo, Consolas, monospace; }\n.msg.user pre { background: #eff6ff; border-left: 3px solid #2563eb; }\n.msg.assistant { border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 12px; background: #fff; }\n.msg.assistant pre.answer { background: #f8fafc; border-left: 3px solid #16a34a; }\n.role-tag { font-size: 12px; font-weight: 600; color: #475569; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }\n.role-tag .perf { font-weight: 400; color: #94a3b8; font-family: ui-monospace, Menlo, monospace; font-size: 11px; }\ndetails.system summary, details.tool summary, details.reasoning summary {\n  cursor: pointer; font-size: 12px; color: #64748b; padding: 4px 0; }\ndetails.system pre { background: #f1f5f9; }\ndetails.tool pre { background: #f8fafc; }\ndetails.reasoning pre { background: #fffbeb; border-left: 3px solid #f59e0b; }\n.tool-call { margin: 6px 0; padding: 8px 10px; background: #f0fdf4; border-left: 3px solid #22c55e; border-radius: 6px; font-size: 12px; }\n.tool-call pre { background: transparent; padding: 6px 0 0; max-height: 200px; }\n.score-box { display: flex; gap: 18px; flex-wrap: wrap; margin: 10px 0; padding: 10px 12px;\n  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 13px; }\n.score-box .k { color: #64748b; font-size: 12px; }\n.patch-pre { background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 6px;\n  font-size: 11px; max-height: 240px; overflow: auto; white-space: pre-wrap; word-break: break-all;\n  font-family: ui-monospace, Menlo, monospace; }\n.skip-list { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px 16px; }\n.skip-item { padding: 6px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px; }\n.skip-item:last-child { border-bottom: none; }\n.toolbar { margin: 10px 0; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }\n.toolbar input { flex: 1; max-width: 360px; padding: 7px 10px; border: 1px solid #e2e8f0; border-radius: 6px; font-size: 13px; }\n.toolbar button { padding: 7px 12px; border: 1px solid #e2e8f0; border-radius: 6px; background: #fff; cursor: pointer; font-size: 13px; }\n.filter-btn { padding: 7px 14px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; cursor: pointer; font-size: 13px; color: #475569; }\n.filter-btn:hover { background: #f1f5f9; }\n.filter-btn.active { background: #2563eb; color: #fff; border-color: #2563eb; }\n.toolbar button:hover { background: #f1f5f9; }\n/* 用户视角报告增强 */\n:root {\n  --primary: #175cd3; --primary-soft: #eff6ff; --success: #067647; --success-soft: #ecfdf3;\n  --warning: #b54708; --warning-soft: #fffaeb; --danger: #b42318; --danger-soft: #fef3f2;\n  --ink: #101828; --muted: #667085; --line: #e4e7ec; --surface: #fff; --canvas: #f7f9fc;\n}\nhtml { scroll-behavior: smooth; scroll-padding-top: 78px; }\nbody { max-width: 1560px; margin: 0 auto; padding: 0 40px 96px; background: var(--canvas); color: var(--ink); }\n.report-hero { margin: 0 -40px; padding: 38px 40px 32px; color: #fff; background: linear-gradient(128deg, #0b1f44 0%, #123d7a 56%, #175cd3 100%); box-shadow: 0 8px 30px rgba(16,24,40,.12); }\n.hero-topline { display: flex; align-items: center; justify-content: space-between; gap: 24px; }\n.hero-kicker { margin: 0 0 8px; color: #b9d5ff; font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }\n.report-hero h1 { margin: 0; color: #fff; font-size: clamp(25px,3vw,38px); line-height: 1.2; letter-spacing: -.02em; }\n.hero-subtitle { max-width: 820px; margin: 12px 0 0; color: #d8e7ff; font-size: 15px; }\n.hero-tags { display: grid; grid-template-columns: minmax(220px,1.3fr) repeat(4,minmax(140px,1fr)); gap: 10px; max-width: 1180px; margin-top: 24px; }\n.hero-tag { min-width: 0; padding: 10px 13px; border: 1px solid rgba(255,255,255,.2); border-radius: 9px; background: rgba(255,255,255,.08); color: #edf4ff; }\n.hero-tag-label { display: block; margin-bottom: 3px; color: #b9d5ff; font-size: 10px; font-weight: 700; letter-spacing: .08em; }\n.hero-tag-value { display: block; color: #fff; font-size: 13px; font-weight: 700; line-height: 1.4; white-space: normal; }\n.hero-model { color: #9ee7c0; }\n.hero-dataset { color: #fff; }\n.hero-action { flex: none; padding: 9px 14px; border: 1px solid rgba(255,255,255,.35); border-radius: 8px; color: #fff; background: rgba(255,255,255,.1); cursor: pointer; font-weight: 600; }\n.hero-action:hover { background: rgba(255,255,255,.2); }\n.section-nav { position: sticky; top: 0; z-index: 50; display: flex; gap: 4px; margin: 0 -40px 28px; padding: 9px 40px; overflow-x: auto; border-bottom: 1px solid var(--line); background: rgba(255,255,255,.94); box-shadow: 0 3px 12px rgba(16,24,40,.06); backdrop-filter: blur(12px); }\n.section-nav a { flex: none; padding: 7px 11px; border-radius: 7px; color: #475467; text-decoration: none; font-size: 13px; font-weight: 600; }\n.section-nav a:hover { color: var(--primary); background: var(--primary-soft); }\nh2 { margin: 44px 0 14px; padding: 0; border: 0; color: #182230; font-size: 22px; letter-spacing: -.01em; }\nh2::before { content: ""; display: inline-block; width: 4px; height: 19px; margin-right: 10px; border-radius: 4px; background: var(--primary); vertical-align: -2px; }\nh3 { margin-top: 26px; color: #344054; font-size: 16px; }\n.report-meta { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: -12px 0 20px; color: var(--muted); font-size: 12px; }\n.report-meta span { display: inline-flex; align-items: center; gap: 6px; }\n.report-meta b { color: #344054; }\n.decision-panel { display: grid; grid-template-columns: minmax(0,1.35fr) minmax(300px,.65fr); gap: 16px; margin: 16px 0 20px; }\n.decision-main, .decision-side, .method-card, .glossary { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); box-shadow: 0 2px 8px rgba(16,24,40,.04); }\n.decision-main { padding: 22px 24px; border-left: 5px solid var(--warning); }\n.decision-label { color: var(--warning); font-size: 12px; font-weight: 800; letter-spacing: .08em; }\n.decision-title { margin: 6px 0 8px; font-size: 21px; line-height: 1.35; }\n.decision-copy { margin: 0; color: #475467; }\n.decision-side { padding: 18px 20px; }\n.decision-side .side-title { margin-bottom: 10px; color: #344054; font-weight: 700; }\n.decision-side ul { margin: 0; padding-left: 18px; color: #475467; }\n.decision-side li + li { margin-top: 7px; }\n.kpi-grid { display: grid; grid-template-columns: repeat(6,minmax(0,1fr)); gap: 12px; margin: 16px 0; }\n.kpi-card { min-height: 126px; padding: 16px; border: 1px solid var(--line); border-radius: 12px; background: #fff; box-shadow: 0 2px 8px rgba(16,24,40,.04); }\n.kpi-card.primary { color: #fff; border-color: transparent; background: linear-gradient(145deg,#1849a9,#175cd3); }\n.kpi-label { min-height: 36px; color: var(--muted); font-size: 12px; font-weight: 600; }\n.kpi-card.primary .kpi-label, .kpi-card.primary .kpi-note { color: #d8e7ff; }\n.kpi-value { margin: 5px 0 2px; color: #101828; font-size: 27px; font-weight: 750; line-height: 1.1; font-variant-numeric: tabular-nums; }\n.kpi-card.primary .kpi-value { color: #fff; }\n.kpi-value.good { color: var(--success); } .kpi-value.risk { color: var(--danger); }\n.kpi-note { color: #98a2b3; font-size: 11px; }\n.outcome-card { display: grid; grid-template-columns: minmax(240px,.72fr) minmax(0,1.28fr); gap: 24px; align-items: center; margin: 18px 0; padding: 20px 22px; border: 1px solid var(--line); border-radius: 12px; background: #fff; }\n.outcome-title { margin: 0 0 4px; font-size: 15px; }\n.outcome-caption { color: var(--muted); font-size: 12px; }\n.outcome-bar { display: flex; height: 16px; overflow: hidden; border-radius: 999px; background: #eaecf0; }\n.outcome-bar .pass { width: 85.827%; background: #17b26a; } .outcome-bar .fail { width: 13.597%; background: #f04438; } .outcome-bar .skip { width: .576%; min-width: 5px; background: #f79009; }\n.outcome-legend { display: flex; flex-wrap: wrap; gap: 10px 18px; margin-top: 10px; color: #475467; font-size: 12px; }\n.legend-dot { display: inline-block; width: 8px; height: 8px; margin-right: 6px; border-radius: 50%; }\n.section-intro { max-width: 920px; margin: -6px 0 18px; color: var(--muted); }\n.metric-table { border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 2px 8px rgba(16,24,40,.035); }\n.metric-table thead tr th, .metric-table thead tr:first-child th { color: #344054; border-color: var(--line); background: #f2f4f7; font-size: 12px; }\n.metric-table tbody tr:hover { background: #f9fafb; }\n.metric-table tbody td { border-color: #eaecf0; }\n.performance-panel { overflow: hidden; margin: 16px 0 28px; border: 1px solid var(--line); border-radius: 14px; background: #fff; box-shadow: 0 3px 12px rgba(16,24,40,.045); }\n.performance-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 16px 20px; border-bottom: 1px solid var(--line); background: linear-gradient(135deg,#f8fbff,#fff); }\n.performance-head h3 { margin: 0 0 3px; color: #182230; font-size: 15px; }\n.performance-head p { margin: 0; color: var(--muted); font-size: 11px; }\n.performance-sample { flex: none; padding: 5px 9px; border: 1px solid #d1e9ff; border-radius: 999px; color: #175cd3; background: #eff8ff; font-size: 10px; font-weight: 700; }\n.performance-table-wrap { overflow-x: auto; }\n.performance-table { min-width: 780px; margin: 0; border: 0; border-radius: 0; box-shadow: none; }\n.performance-table thead th { padding: 10px 14px; text-align: center; }\n.performance-table thead th:first-child { width: 40%; text-align: left; }\n.performance-table thead th:nth-child(2) { width: 80px; }\n.performance-table tbody td { padding: 11px 14px; text-align: center; font-weight: 700; font-variant-numeric: tabular-nums; }\n.performance-table tbody td:first-child { text-align: left; font-weight: 400; }\n.performance-table tbody tr:nth-child(2) { background: #fbfcfe; }\n.metric-primary { color: #344054; font-size: 12px; font-weight: 720; }\n.metric-secondary { margin-top: 3px; color: #98a2b3; font-size: 10px; font-weight: 400; line-height: 1.35; }\n.unit-pill { display: inline-block; min-width: 34px; padding: 3px 7px; border-radius: 999px; color: #475467; background: #f2f4f7; font-size: 10px; font-weight: 700; }\n.domain-cell { min-width: 220px; }\n.domain-name { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 5px; }\n.domain-track { height: 6px; overflow: hidden; border-radius: 999px; background: #eaecf0; }\n.domain-fill { height: 100%; border-radius: inherit; background: var(--primary); }\n.domain-fill.good { background: #17b26a; } .domain-fill.risk { background: #f79009; }\n.method-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; margin: 16px 0 22px; }\n.method-card { padding: 16px; }\n.method-step { color: var(--primary); font-size: 12px; font-weight: 800; }\n.method-card h4 { margin: 5px 0 7px; font-size: 14px; }\n.method-card p { margin: 0; color: var(--muted); font-size: 12px; }\n.glossary { margin: 16px 0 24px; padding: 16px 18px; }\n.glossary summary { cursor: pointer; color: #344054; font-weight: 700; }\n.glossary-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 12px 24px; margin-top: 14px; }\n.glossary-item b { display: block; color: #344054; font-size: 12px; }\n.glossary-item span { color: var(--muted); font-size: 12px; }\n.summary-grid { grid-template-columns: repeat(4,minmax(0,1fr)); }\n.config-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(min(100%,480px),1fr)); gap: 18px; align-items: start; margin: 18px 0 8px; }\n.config-block { min-width: 0; overflow: hidden; border: 1px solid #dfe4ec; border-radius: 16px; background: #fff; box-shadow: 0 4px 14px rgba(16,24,40,.055); }\n.config-header { display: flex; align-items: flex-start; gap: 12px; min-height: 90px; margin: 0; padding: 18px 20px; border-bottom: 1px solid var(--line); background: linear-gradient(135deg,#f8fbff 0%,#fff 72%); }\n.config-block.judge .config-header { background: linear-gradient(135deg,#f8fafc 0%,#fff 72%); }\n.config-icon { display: grid; flex: none; width: 38px; height: 38px; place-items: center; border-radius: 10px; color: #fff; background: var(--primary); font-size: 13px; font-weight: 800; box-shadow: 0 4px 10px rgba(23,92,211,.2); }\n.config-block.judge .config-icon { background: #475467; box-shadow: 0 4px 10px rgba(71,84,103,.18); }\n.config-heading { min-width: 0; flex: 1; }\n.config-eyebrow { margin-bottom: 3px; color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .09em; }\n.config-block.judge .config-eyebrow { color: #667085; }\n.config-title { overflow-wrap: anywhere; color: #182230; font-size: 17px; font-weight: 760; line-height: 1.3; }\n.config-sub { margin-top: 4px; color: var(--muted); font-size: 11px; line-height: 1.45; }\n.config-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }\n.config-tag { padding: 3px 7px; border: 1px solid #d1e9ff; border-radius: 999px; color: #175cd3; background: #eff8ff; font-size: 10px; font-weight: 650; }\n.config-block.judge .config-tag { color: #475467; border-color: #e4e7ec; background: #f9fafb; }\n.config-body { padding: 4px 18px 18px; }\n.config-section { margin: 12px 0 0; overflow: hidden; border: 1px solid #eaecf0; border-radius: 11px; background: #fff; }\n.config-section + .config-section { margin-top: 10px; }\n.config-section summary { display: flex; align-items: center; gap: 8px; padding: 10px 12px; cursor: pointer; list-style: none; color: #344054; background: #f9fafb; font-size: 12px; font-weight: 720; user-select: none; }\n.config-section summary::-webkit-details-marker { display: none; }\n.config-section summary::before { content: "›"; color: #667085; font-size: 18px; line-height: 1; transition: transform .16s ease; }\n.config-section[open] summary::before { transform: rotate(90deg); }\n.config-section-count { margin-left: auto; padding: 2px 7px; border-radius: 999px; color: #667085; background: #eaecf0; font-size: 10px; font-weight: 650; }\n.config-block .metric-table { margin: 0; border: 0; border-radius: 0; box-shadow: none; table-layout: fixed; }\n.config-block .metric-table tbody td:first-child { width: 40%; color: #667085; background: #fcfcfd; font-size: 11px; font-weight: 600; }\n.config-block .metric-table tbody td { padding: 8px 12px; border-bottom: 1px solid #f2f4f7; vertical-align: top; }\n.config-block .metric-table tbody tr:last-child td { border-bottom: 0; }\n.config-block .metric-table tbody tr:hover td { background: #f8fbff; }\n.config-block .metric-table tbody td.mono { overflow-wrap: anywhere; text-align: left; color: #182230; font-size: 11px; font-weight: 650; line-height: 1.45; }\n.config-value-strong { color: var(--primary); font-weight: 800; }\n.config-row-highlight { background: #f5f8ff; }\n.config-row-highlight td.mono { color: var(--primary); font-weight: 800; }\n.kpi-group { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 16px; margin: 16px 0 8px; }\n.kpi-block { padding: 18px 20px; border: 1px solid var(--line); border-radius: 14px; background: #fff; box-shadow: 0 2px 8px rgba(16,24,40,.04); }\n.kpi-block-title { display: flex; align-items: center; gap: 8px; margin: 0 0 14px; color: #344054; font-size: 14px; font-weight: 700; letter-spacing: .01em; }\n.kpi-block-title::before { content: ""; display: inline-block; width: 4px; height: 14px; border-radius: 3px; background: var(--primary); }\n.kpi-block-title .meta { margin-left: auto; color: var(--muted); font-size: 11px; font-weight: 500; }\n.kpi-mini-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; }\n.kpi-card { min-height: 108px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 12px; background: #fff; box-shadow: 0 1px 4px rgba(16,24,40,.035); display: flex; flex-direction: column; justify-content: space-between; }\n.kpi-card .kpi-value { font-size: 24px; font-weight: 750; }\n.kpi-card .kpi-label { color: #475467; font-size: 12px; font-weight: 600; }\n.kpi-card .kpi-note { color: #98a2b3; font-size: 11px; }\n.kpi-pair { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: #fff; box-shadow: 0 1px 4px rgba(16,24,40,.035); }\n.kpi-pair .pair-title { color: #475467; font-size: 12px; font-weight: 600; }\n.kpi-pair .pair-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }\n.kpi-pair .pair-label { color: #98a2b3; font-size: 11px; font-weight: 600; }\n.kpi-pair .pair-value { color: #101828; font-size: 18px; font-weight: 750; font-variant-numeric: tabular-nums; }\n.kpi-pair .pair-value.good { color: var(--success); }\n.kpi-pair .pair-value.warn { color: var(--warning); }\n.kpi-mini-grid.pairs { grid-template-columns: repeat(3,minmax(0,1fr)); gap: 10px; }\n.stat-card { border-color: var(--line); border-radius: 10px; box-shadow: 0 2px 8px rgba(16,24,40,.035); }\n.issue-toolbar { position: sticky; top: 57px; z-index: 40; margin: 12px 0 0; padding: 12px; border: 1px solid var(--line); border-radius: 10px 10px 0 0; background: rgba(255,255,255,.96); box-shadow: 0 4px 12px rgba(16,24,40,.06); backdrop-filter: blur(10px); }\n.issue-toolbar .search-box { flex: 1 1 280px; max-width: 460px; height: 36px; padding: 8px 11px; border: 1px solid #d0d5dd; border-radius: 8px; color: #344054; background: #fff; }\n.issue-toolbar select { height: 36px; padding: 0 32px 0 10px; border: 1px solid #d0d5dd; border-radius: 8px; color: #344054; background: #fff; }\n.issue-count { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }\n.issue-summary { margin-top: 0; border-color: var(--line); border-radius: 0 0 10px 10px; table-layout: auto; }\n.issue-summary thead { position: static; }\n.issue-summary th { position: static; background: #f2f4f7; }\n.issue-summary th:nth-child(2), .issue-summary td:nth-child(2) { max-width: 480px; } .issue-summary td:nth-child(2) { min-width: 360px; } .issue-summary th:nth-child(5), .issue-summary th:nth-child(6), .issue-summary th:nth-child(7), .issue-summary th:nth-child(8), .issue-summary th:nth-child(9), .issue-summary th:nth-child(10) { white-space: normal; line-height: 1.3; padding-top: 8px; padding-bottom: 8px; }\n.issue-summary td:nth-child(2) code { display: block; overflow: hidden; color: #344054; text-overflow: ellipsis; white-space: nowrap; }\n.case-name { display: flex; align-items: flex-start; gap: 6px; min-width: 0; }\n.case-name .caret { flex: none; margin-top: 1px; }\n.case-title { display: -webkit-box; overflow: hidden; color: #344054; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 12px; font-weight: 600; line-height: 1.45; text-align: left; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }\n.issue-row:hover .case-title { color: #175cd3; }\n.case-group-row { cursor: pointer; background: #fff; }\n.case-group-row:hover { background: #f5f8ff; }\n.case-group-row:focus { outline: 2px solid var(--primary); outline-offset: -2px; }\n.case-group-row td { padding-top: 13px; padding-bottom: 13px; border-top: 1px solid #d0d5dd; }\n.case-group-row .group-caret { display: inline-block; flex: none; width: 14px; color: #667085; transition: transform .15s; }\n.case-group-row.open .group-caret { transform: rotate(90deg); }\n.case-group-row .group-caret-pass { color: var(--success) !important; }\n.case-group-row .group-caret-fail { color: var(--danger) !important; }\n.case-group-row .group-caret-skip { color: var(--warning) !important; }\n.group-index { color: #475467; font-size: 11px; font-weight: 700; white-space: nowrap; }\n.group-name { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }\n.group-title-wrap { min-width: 0; }\n.group-title { display: -webkit-box; overflow: hidden; color: #182230; font-size: 12px; font-weight: 700; line-height: 1.45; text-align: left; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }\n.group-meta { margin-top: 4px; color: #98a2b3; font-size: 10px; text-align: left; }\n.execution-count { display: inline-block; min-width: 42px; color: #344054; font-size: 12px; font-weight: 700; text-align: center; white-space: nowrap; }\n.group-result { white-space: nowrap; }\n.aggregate-badge { display: inline-block; min-width: 64px; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 750; text-align: center; white-space: nowrap; }\n.aggregate-badge.pass { color: var(--success); background: var(--success-soft); }\n.aggregate-badge.fail { color: var(--danger); background: var(--danger-soft); }\n.aggregate-badge.skip { color: var(--warning); background: var(--warning-soft); }\n.aggregate-badge { display: inline-flex; gap: 4px; align-items: center; justify-content: center; }\n.agg-part { padding: 1px 5px; border-radius: 999px; font-weight: 750; }\n.agg-part.pass { color: #067647; background: #d1fadf; }\n.agg-part.fail { color: #b42318; background: #fee4e2; }\n.agg-part.skip { color: #b54708; background: #fef0c7; }\n.issue-row.group-run { display: none; background: #fbfcfe; }\n.issue-row.group-run:hover { background: #f1f6ff; }\n.issue-row.group-run td { color: #475467; border-bottom-color: #eaecf0; }\n.issue-row.group-run td:first-child { padding-left: 18px; color: #667085; font-size: 11px; font-weight: 700; white-space: nowrap; }\n.run-name { display: flex; align-items: center; gap: 7px; color: #475467; font-size: 11px; font-weight: 600; }\n.run-name .caret { flex: none; }\n.run-sample { color: #98a2b3; font-family: ui-monospace, Menlo, monospace; font-size: 10px; font-weight: 400; }\n.issue-row.group-run.open { background: #eef4ff; }\n.issue-detail.group-detail td { border-left-color: #84adff; }\n.issue-detail.group-detail td { border-left-color: #84adff; }\n.skipped-summary { margin: 18px 0 8px; padding: 18px 20px; border: 1px solid #fda29b; border-radius: 14px; background: linear-gradient(180deg,#fef3f2,#fff 78%); box-shadow: 0 3px 12px rgba(180,35,24,.07); }\n.skipped-summary-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }\n.skipped-summary-title { display: flex; align-items: center; gap: 8px; color: #b42318; font-size: 14px; font-weight: 750; }\n.skipped-icon { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; color: #fff; background: #d92d20; font-size: 13px; font-weight: 800; }\n.skipped-summary-link { color: #175cd3; font-size: 12px; font-weight: 600; text-decoration: none; }\n.skipped-summary-link:hover { text-decoration: underline; }\n.skipped-summary-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 10px; margin-bottom: 14px; }\n.skipped-stat { padding: 10px 12px; border: 1px solid #fda29b; border-radius: 10px; background: #fff; }\n.skipped-stat .k { color: #667085; font-size: 11px; font-weight: 650; }\n.skipped-stat .v { margin-top: 4px; color: #b42318; font-size: 13px; font-weight: 750; }\n.skipped-cause-title { margin: 14px 0 6px; color: #344054; font-size: 12px; font-weight: 720; letter-spacing: .01em; }\n.skipped-cause { margin: 0 0 8px; overflow-wrap: anywhere; color: #475467; font-size: 12px; line-height: 1.65; }\n.skipped-cause code { padding: 1px 5px; border-radius: 4px; background: #f2f4f7; color: #b42318; font-size: 11px; }\n.skipped-distribution { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px dashed #eaecf0; }\n.skipped-distribution:last-child { border-bottom: 0; }\n.skipped-domain { color: #475467; font-size: 12px; font-weight: 650; }\n.skipped-count { margin-left: auto; color: #b42318; font-size: 12px; font-weight: 750; }\n.skipped-actions { padding-left: 18px; color: #475467; font-size: 12px; line-height: 1.7; }\n.skipped-actions li::marker { color: #b42318; }\n.skipped-root { margin: 4px 0 6px; padding: 12px 14px; border: 1px solid #fda29b; border-radius: 10px; background: #fff; }\n.skipped-root-label { color: #b42318; font-size: 11px; font-weight: 700; }\n.skipped-root-tag { display: inline-block; margin: 6px 0 4px; padding: 3px 8px; border-radius: 999px; color: #fff; background: #d92d20; font-size: 11px; font-weight: 750; }\n.skipped-root-text { color: #344054; font-size: 12px; line-height: 1.6; }\n.skipped-details { margin-top: 10px; border: 1px solid #fda29b; border-radius: 10px; background: #fff; }\n.skipped-details summary { padding: 10px 14px; cursor: pointer; color: #b42318; font-size: 12px; font-weight: 700; list-style: none; user-select: none; }\n.skipped-details summary::-webkit-details-marker { display: none; }\n.skipped-details summary::before { content: "▸"; margin-right: 8px; font-size: 12px; transition: transform .15s ease; }\n.skipped-details[open] summary::before { transform: rotate(90deg); }\n.skipped-details-body { padding: 4px 14px 14px; border-top: 1px solid #fee4e2; }\n.skipped-details-body > :first-child { margin-top: 8px; }\n@media (max-width: 760px) { .skipped-summary-grid { grid-template-columns: 1fr; } }\n.failure-diagnostic { margin: 12px 0 16px; overflow: hidden; border: 1px solid #fda29b; border-radius: 12px; background: #fff; box-shadow: 0 3px 12px rgba(180,35,24,.07); }\n.failure-diagnostic-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 14px; color: #912018; border-bottom: 1px solid #fee4e2; background: #fef3f2; }\n.failure-diagnostic-head > div { display: flex; align-items: center; gap: 8px; }\n.failure-icon { display: grid; width: 20px; height: 20px; place-items: center; border-radius: 50%; color: #fff; background: #d92d20; font-size: 12px; font-weight: 800; }\n.failure-source { color: #b42318; font-size: 10px; font-weight: 500; }\n.failure-status { padding: 3px 8px; border-radius: 999px; color: #b42318; background: #fee4e2; font-size: 10px; font-weight: 800; }\n.failure-reason { display: grid; grid-template-columns: 82px minmax(0,1fr); gap: 10px; padding: 13px 14px; border-bottom: 1px solid #f2f4f7; }\n.failure-label { color: #667085; font-size: 11px; font-weight: 750; }\n.failure-text, .answer-value { overflow-wrap: anywhere; color: #344054; font-size: 12px; line-height: 1.65; white-space: pre-wrap; }\n.answer-compare { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 0; }\n.answer-card { min-width: 0; padding: 13px 14px 15px; }\n.answer-card + .answer-card { border-left: 1px solid #eaecf0; }\n.answer-card.expected { background: #f6fef9; }\n.answer-card.actual { background: #fffafa; }\n.answer-card.expected .failure-label { color: #067647; }\n.answer-card.actual .failure-label { color: #b42318; }\n.answer-card .answer-value { margin-top: 7px; max-height: 150px; overflow: auto; }\n.issue-detail .score-box { margin-top: 10px; opacity: .82; }\n@media (max-width: 760px) { .answer-compare { grid-template-columns: 1fr; } .answer-card + .answer-card { border-left: 0; border-top: 1px solid #eaecf0; } .failure-reason { grid-template-columns: 1fr; gap: 5px; } }\n.issue-row:hover { background: #f5f8ff; }\n.issue-row:focus { outline: 2px solid var(--primary); outline-offset: -2px; }\n.issue-detail td { padding: 18px; border-left: 3px solid #d1e9ff; background: #fbfcfe; }\n.badge { border-radius: 999px; font-weight: 750; letter-spacing: .02em; }\n.badge.pass { color: var(--success); background: var(--success-soft); }\n.badge.fail { color: var(--danger); background: var(--danger-soft); }\n.badge.skip { color: var(--warning); background: var(--warning-soft); }\n.task-card { border-color: var(--line); box-shadow: 0 2px 8px rgba(16,24,40,.04); }\n.msg.assistant { border-color: var(--line); }\n.back-to-top { position: fixed; right: 24px; bottom: 24px; z-index: 80; width: 42px; height: 42px; border: 0; border-radius: 50%; color: #fff; background: #175cd3; box-shadow: 0 6px 18px rgba(23,92,211,.3); cursor: pointer; opacity: 0; pointer-events: none; transition: .2s; }\n.kpi-link-alert { display: inline-block; padding: 1px 8px; margin: -1px -2px -1px 2px; border: 1px solid #fda29b; border-radius: 999px; color: #b42318; background: #fef3f2; font-size: 11px; font-weight: 800; text-decoration: none; transition: .15s; }\n.kpi-link-alert:hover { color: #fff; background: #d92d20; border-color: #d92d20; }\n.kpi-link-alert:focus-visible { outline: 2px solid #d92d20; outline-offset: 2px; }\n.back-to-top.show { opacity: 1; pointer-events: auto; }\n.empty-state { display: none; padding: 28px; text-align: center; color: var(--muted); border: 1px dashed #d0d5dd; border-top: 0; background: #fff; }\n@media (max-width: 1180px) { .kpi-grid { grid-template-columns: repeat(3,1fr); } .method-grid { grid-template-columns: repeat(2,1fr); } .hero-tags { grid-template-columns: repeat(2,minmax(0,1fr)); max-width: none; } .kpi-group { grid-template-columns: 1fr 1fr; } .kpi-mini-grid.pairs { grid-template-columns: 1fr 1fr; } .config-grid { grid-template-columns: 1fr; } }\n@media (max-width: 760px) {\n  body { padding: 0 16px 72px; } .report-hero, .section-nav { margin-left: -16px; margin-right: -16px; padding-left: 16px; padding-right: 16px; }\n  .hero-topline { align-items: flex-start; flex-direction: column; } .hero-tags { grid-template-columns: 1fr; width: 100%; } .decision-panel, .outcome-card { grid-template-columns: 1fr; }\n  .kpi-grid, .summary-grid, .method-grid, .glossary-grid, .kpi-group { grid-template-columns: 1fr 1fr; } .kpi-mini-grid, .kpi-mini-grid.pairs { grid-template-columns: 1fr; }\n  .issue-summary { display: block; overflow-x: auto; } .issue-count { width: 100%; margin-left: 0; }\n}\n@media (max-width: 480px) { .kpi-grid, .summary-grid, .method-grid, .glossary-grid { grid-template-columns: 1fr; } }\n@media print {\n  body { max-width: none; padding: 0 18px; background: #fff; } .section-nav, .hero-action, .issue-toolbar, .back-to-top { display: none !important; }\n  .report-hero { margin: 0 -18px 24px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }\n  .kpi-grid { grid-template-columns: repeat(3,1fr); } .issue-detail { display: none !important; }\n}\n'
JS = '\nfunction toggleIssue(i) {\n  var row = document.getElementById(\'issue-detail-\' + i);\n  var trigger = row.previousElementSibling;\n  row.classList.toggle(\'show\'); trigger.classList.toggle(\'open\');\n}\nfunction toggleCard(el) { el.parentElement.classList.toggle(\'open\'); }\nfunction expandAll(flag) {\n  document.querySelectorAll(\'.task-card\').forEach(function(c) {\n    c.classList.toggle(\'open\', flag);\n  });\n}\nfunction filterCards() {\n  var kw = document.getElementById(\'q\').value.trim().toLowerCase();\n  document.querySelectorAll(\'.task-card\').forEach(function(c) {\n    var hay = (c.getAttribute(\'data-search\') || \'\').toLowerCase();\n    c.style.display = hay.indexOf(kw) >= 0 ? \'\' : \'none\';\n  });\n}\nfunction filterResult(flag, btn) {\n  document.querySelectorAll(\'.filter-btn\').forEach(function(b) { b.classList.remove(\'active\'); });\n  if (btn) btn.classList.add(\'active\');\n  document.querySelectorAll(\'tr.issue-row\').forEach(function(r) {\n    var show = (flag === \'all\' || r.getAttribute(\'data-result\') === flag);\n    r.style.display = show ? \'\' : \'none\';\n    var d = r.nextElementSibling;\n    if (d && d.classList.contains(\'issue-detail\')) { d.classList.remove(\'show\'); d.style.display = \'\'; }\n  });\n}\nvar issueFilterState = { result: \'all\', dataset: \'all\', query: \'\' };\nvar issueGroups = [];\n\nfunction toggleIssue(i) {\n  var detail = document.getElementById(\'issue-detail-\' + i);\n  if (!detail) return;\n  var trigger = detail.previousElementSibling;\n  var expanded = !detail.classList.contains(\'show\');\n  detail.classList.toggle(\'show\', expanded);\n  detail.style.display = expanded ? \'table-row\' : \'none\';\n  trigger.classList.toggle(\'open\', expanded);\n  trigger.setAttribute(\'aria-expanded\', String(expanded));\n}\n\nfunction parseMetric(value) {\n  var match = String(value || \'\').replace(/,/g, \'\').match(/-?\\d+(?:\\.\\d+)?/);\n  return match ? Number(match[0]) : null;\n}\n\nfunction averageMetric(runs, cellIndex) {\n  var values = runs.map(function(run) { return parseMetric(run.row.cells[cellIndex].textContent); })\n    .filter(function(value) { return value !== null && Number.isFinite(value); });\n  if (!values.length) return null;\n  return values.reduce(function(total, value) { return total + value; }, 0) / values.length;\n}\n\nfunction formatAverage(value, type) {\n  if (value === null) return \'—\';\n  if (type === \'integer\') return Math.round(value).toLocaleString(\'zh-CN\');\n  if (type === \'seconds\') return value.toFixed(2) + \'s\';\n  return value.toFixed(1);\n}\n\nfunction getDataset(detail) {\n  var code = detail.querySelector(\'td > .meta code\');\n  if (code) return code.textContent.replace(\'tau2_bench_\', \'\').trim().toLowerCase();\n  var labels = detail.querySelectorAll(\'.task-meta .k\');\n  for (var i = 0; i < labels.length; i += 1) {\n    if (labels[i].textContent.indexOf(\'数据集\') >= 0) {\n      var value = labels[i].parentElement.querySelector(\'.v\');\n      return value ? value.textContent.trim().toLowerCase() : \'\';\n    }\n  }\n  return \'\';\n}\n\nfunction datasetLabel(dataset) {\n  return { airline: \'航空 Airline\', retail: \'零售 Retail\', telecom: \'通信 Telecom\' }[dataset] || dataset;\n}\n\nfunction groupCode(dataset, index) {\n  var prefix = { airline: \'A\', retail: \'R\', telecom: \'T\' }[dataset] || \'Q\';\n  return prefix + \'-\' + String(index + 1).padStart(3, \'0\');\n}\n\nfunction aggregateResult(runs) {\n  var counts = { pass: 0, fail: 0, skip: 0, score: 0 };\n  runs.forEach(function(run) { counts[run.result] = (counts[run.result] || 0) + 1; });\n  var result = \'score\';\n  if (counts.skip) result = \'skip\';\n  else if (counts.fail) result = \'fail\';\n  else if (counts.pass === runs.length) result = \'pass\';\n  var parts = [];\n  if (counts.pass) parts.push(\'<span class="agg-part pass">\' + counts.pass + \'通过</span>\');\n  if (counts.fail) parts.push(\'<span class="agg-part fail">\' + counts.fail + \'失败</span>\');\n  if (counts.score) parts.push(\'<span class="agg-part score">\' + counts.score + \'轮得分</span>\');\n  if (counts.skip) parts.push(\'<span class="agg-part skip">\' + counts.skip + \'异常</span>\');\n  return {\n    result: result,\n    html: \'<span class="aggregate-badge \' + result + \'">\' + parts.join(\' \') + \'</span>\',\n    counts: counts\n  };\n}\n\n\nfunction setGroupExpanded(group, expanded) {\n  group.expanded = expanded;\n  group.parent.classList.toggle(\'open\', expanded);\n  group.parent.setAttribute(\'aria-expanded\', String(expanded));\n  group.runs.forEach(function(run) {\n    run.row.style.display = expanded ? \'table-row\' : \'none\';\n    if (!expanded) {\n      run.detail.classList.remove(\'show\');\n      run.detail.style.display = \'none\';\n      run.row.classList.remove(\'open\');\n      run.row.setAttribute(\'aria-expanded\', \'false\');\n    } else {\n      run.detail.style.display = run.detail.classList.contains(\'show\') ? \'table-row\' : \'none\';\n    }\n  });\n}\n\nfunction toggleGroup(key) {\n  var group = issueGroups.find(function(item) { return item.key === key; });\n  if (group) setGroupExpanded(group, !group.expanded);\n}\n\nvar issueReps = 1;\n\nfunction escapeHtml(text) {\n  return String(text).replace(/&/g, \'&amp;\').replace(/</g, \'&lt;\').replace(/>/g, \'&gt;\');\n}\n\nfunction escapeAttr(text) {\n  return escapeHtml(text).replace(/"/g, \'&quot;\');\n}\n\nfunction initializeIssueGroups() {\n  var table = document.querySelector(\'table.issue-summary\');\n  if (!table) return;\n  issueReps = Number(table.getAttribute(\'data-reps\')) || 1;\n  var colCount = table.tHead && table.tHead.rows[0] ? table.tHead.rows[0].cells.length : 0;\n  if (issueReps <= 1) {\n    // 单轮执行：保留静态行（列数与表头一致），筛选由 applyIssueFilters 按行处理\n    issueGroups = [];\n    return;\n  }\n  var tbody = table.tBodies[0];\n  var rows = Array.from(tbody.querySelectorAll(\'tr.issue-row\'));\n  var groupsByKey = new Map();\n\n  rows.forEach(function(row) {\n    var detail = row.nextElementSibling;\n    if (!detail || !detail.classList.contains(\'issue-detail\')) return;\n    var sampleText = row.cells[0].textContent.trim();\n    var sampleId = Number(sampleText.replace(/^S/, \'\'));\n    if (!Number.isFinite(sampleId)) sampleId = 0;\n    var dataset = row.getAttribute(\'data-dataset\') || getDataset(detail);\n    var taskIndex = Math.floor(sampleId / issueReps);\n    var key = dataset + \'-\' + taskIndex;\n    var titleNode = row.querySelector(\'.case-title\');\n    var title = titleNode ? titleNode.textContent.trim() : row.cells[1].textContent.trim();\n    var group = groupsByKey.get(key);\n    if (!group) {\n      group = { key: key, dataset: dataset, taskIndex: taskIndex, title: title, runs: [], expanded: false };\n      groupsByKey.set(key, group);\n    }\n    group.runs.push({\n      row: row,\n      detail: detail,\n      sampleId: sampleId,\n      repeat: sampleId % issueReps,\n      result: row.getAttribute(\'data-result\') || \'unknown\'\n    });\n  });\n\n  issueGroups = Array.from(groupsByKey.values()).sort(function(a, b) {\n    return String(a.dataset).localeCompare(String(b.dataset)) || (a.taskIndex - b.taskIndex);\n  });\n\n  var perfTypes = [\'decimal\', \'seconds\', \'integer\', \'integer\', \'decimal\', \'seconds\'];\n  var fragment = document.createDocumentFragment();\n  issueGroups.forEach(function(group) {\n    group.runs.sort(function(a, b) { return a.repeat - b.repeat; });\n    var aggregate = aggregateResult(group.runs);\n    group.result = aggregate.result;\n    group.queryText = (groupCode(group.dataset, group.taskIndex) + \' \' + group.title).toLowerCase();\n\n    var parent = document.createElement(\'tr\');\n    parent.className = \'case-group-row\';\n    parent.setAttribute(\'data-result\', group.result);\n    parent.setAttribute(\'data-dataset\', group.dataset);\n    parent.setAttribute(\'tabindex\', \'0\');\n    parent.setAttribute(\'role\', \'button\');\n    parent.setAttribute(\'aria-expanded\', \'false\');\n    parent.onclick = function() { toggleGroup(group.key); };\n    parent.onkeydown = function(event) {\n      if (event.key === \'Enter\' || event.key === \' \') { event.preventDefault(); toggleGroup(group.key); }\n    };\n\n    var executed = group.runs.filter(function(run) { return run.result !== \'skip\'; }).length;\n    // 父行列数与表头严格一致：题号 | 题目 | 执行结果(聚合徽章) | (有 perf 时) 6 列均值\n    var parentHtml =\n      \'<td class="num"><span class="group-index">\' + groupCode(group.dataset, group.taskIndex) + \'</span></td>\' +\n      \'<td><div class="group-name"><span class="group-caret group-caret-\' + aggregate.result + \'">▶</span><div class="group-title-wrap"><div class="group-title" title="\' + escapeAttr(group.title) + \'">\' + escapeHtml(group.title) + \'</div><div class="group-meta">\' + escapeHtml(datasetLabel(group.dataset)) + \' · 共执行 \' + group.runs.length + \' 轮 · \' + executed + \' 轮有效</div></div></div></td>\' +\n      \'<td class="num group-result">\' + aggregate.html + \'</td>\';\n    if (colCount > 3) {\n      for (var ci = 3; ci < colCount && ci - 3 < perfTypes.length; ci += 1) {\n        parentHtml += \'<td class="num" title="有效轮次平均">\' + formatAverage(averageMetric(group.runs, ci), perfTypes[ci - 3]) + \'</td>\';\n      }\n    }\n    parent.innerHTML = parentHtml;\n    group.parent = parent;\n    fragment.appendChild(parent);\n\n    group.runs.forEach(function(run) {\n      run.row.classList.add(\'group-run\');\n      run.row.setAttribute(\'data-dataset\', group.dataset);\n      run.row.setAttribute(\'data-group\', group.key);\n      run.row.setAttribute(\'tabindex\', \'0\');\n      run.row.setAttribute(\'role\', \'button\');\n      run.row.setAttribute(\'aria-expanded\', \'false\');\n      run.row.cells[0].innerHTML = \'<span class="run-sample">第\' + (run.repeat + 1) + \'轮</span>\';\n      run.row.cells[1].innerHTML = \'<div class="run-name"><span class="caret">▶</span><span>执行明细</span></div>\';\n      var resultBadge = run.row.cells[2].querySelector(\'.badge\');\n      if (resultBadge) {\n        var label = { pass: \'通过\', fail: \'失败\', skip: \'异常\' }[run.result];\n        if (label) resultBadge.textContent = label;\n      }\n      run.row.style.display = \'none\';\n      run.detail.cells[0].colSpan = colCount;\n      run.detail.classList.add(\'group-detail\');\n      run.detail.setAttribute(\'data-group\', group.key);\n      run.detail.style.display = \'none\';\n      run.row.addEventListener(\'keydown\', function(event) {\n        if (event.key === \'Enter\' || event.key === \' \') { event.preventDefault(); run.row.click(); }\n      });\n      fragment.appendChild(run.row);\n      fragment.appendChild(run.detail);\n    });\n  });\n  tbody.replaceChildren(fragment);\n}\n\n\nfunction applyIssueFilters() {\n  var visible = 0;\n  var total = 0;\n  if (issueGroups.length) {\n    total = issueGroups.length;\n    issueGroups.forEach(function(group) {\n      var resultMatch = issueFilterState.result === \'all\' || group.result === issueFilterState.result;\n      var datasetMatch = issueFilterState.dataset === \'all\' || group.dataset === issueFilterState.dataset;\n      var queryMatch = !issueFilterState.query || group.queryText.indexOf(issueFilterState.query) >= 0;\n      var show = resultMatch && datasetMatch && queryMatch;\n      group.parent.style.display = show ? \'table-row\' : \'none\';\n      if (!show) setGroupExpanded(group, false);\n      if (show) visible += 1;\n    });\n  } else {\n    document.querySelectorAll(\'table.issue-summary tr.issue-row\').forEach(function(row) {\n      total += 1;\n      var result = row.getAttribute(\'data-result\') || \'\';\n      var dataset = row.getAttribute(\'data-dataset\') || \'\';\n      var queryText = (((row.cells[0] || {}).textContent || \'\') + \' \' + ((row.cells[1] || {}).textContent || \'\')).toLowerCase();\n      var show = (issueFilterState.result === \'all\' || result === issueFilterState.result)\n        && (issueFilterState.dataset === \'all\' || dataset === issueFilterState.dataset)\n        && (!issueFilterState.query || queryText.indexOf(issueFilterState.query) >= 0);\n      row.style.display = show ? \'\' : \'none\';\n      if (!show) {\n        var detail = row.nextElementSibling;\n        if (detail && detail.classList.contains(\'issue-detail\')) {\n          detail.classList.remove(\'show\');\n          detail.style.display = \'none\';\n          row.classList.remove(\'open\');\n        }\n      }\n      if (show) visible += 1;\n    });\n  }\n  var count = document.getElementById(\'issueCount\');\n  if (count) {\n    var text = \'显示 \' + visible.toLocaleString(\'zh-CN\') + \' / \' + total.toLocaleString(\'zh-CN\') + \' 道题\';\n    if (issueGroups.length && issueReps > 1) text += \' · \' + (visible * issueReps).toLocaleString(\'zh-CN\') + \' 轮\';\n    count.textContent = text;\n  }\n  var empty = document.getElementById(\'issueEmpty\');\n  if (empty) empty.style.display = visible ? \'none\' : \'block\';\n}\n\n\nfunction filterResult(flag, btn) {\n  issueFilterState.result = flag;\n  document.querySelectorAll(\'.result-filter\').forEach(function(button) { button.classList.remove(\'active\'); });\n  if (btn) btn.classList.add(\'active\');\n  applyIssueFilters();\n}\n\nfunction filterIssues() {\n  var input = document.getElementById(\'issueSearch\');\n  issueFilterState.query = input ? input.value.trim().toLowerCase() : \'\';\n  applyIssueFilters();\n}\n\nfunction filterDataset() {\n  var select = document.getElementById(\'datasetFilter\');\n  issueFilterState.dataset = select ? select.value : \'all\';\n  applyIssueFilters();\n}\n\nfunction collapseIssues() {\n  if (issueGroups.length) {\n    issueGroups.forEach(function(group) { setGroupExpanded(group, false); });\n    return;\n  }\n  document.querySelectorAll(\'table.issue-summary tr.issue-row.open\').forEach(function(row) { row.classList.remove(\'open\'); });\n  document.querySelectorAll(\'table.issue-summary tr.issue-detail.show\').forEach(function(detail) {\n    detail.classList.remove(\'show\');\n    detail.style.display = \'none\';\n  });\n}\n\n\ndocument.addEventListener(\'DOMContentLoaded\', function() {\n  initializeIssueGroups();\n  applyIssueFilters();\n  var backToTop = document.getElementById(\'backToTop\');\n  window.addEventListener(\'scroll\', function() {\n    if (backToTop) backToTop.classList.toggle(\'show\', window.scrollY > 700);\n  }, { passive: true });\n});\n'


# ==================== 工具函数 ====================

def fmt(v, d=2):
    try:
        return f"{float(v):.{d}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_tok(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def fmt_dur(v):
    try:
        v = float(v)
        return f"{v:.1f}s" if v < 60 else f"{v / 60:.1f}m"
    except (TypeError, ValueError):
        return "—"


def pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def clip(text, limit=None):
    if limit is None:
        limit = MAX_TEXT_CHARS
    if text is None:
        return "", False
    text = str(text)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _content_list_text(content):
    if not isinstance(content, list):
        return ""
    texts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(item.get("text", ""))
    return "\n".join(t for t in texts if t)


_SENSITIVE = ("api_key", "secret", "password", "access_key")


def _mask_secret(k, v):
    kl = str(k).lower()
    if any(t in kl for t in _SENSITIVE):
        s = str(v)
        return (s[:6] + "****") if len(s) > 6 else "****"
    return v


def _flatten(d, prefix=""):
    if isinstance(d, dict):
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                yield from _flatten(v, path)
            elif isinstance(v, list):
                yield path, json.dumps(v, ensure_ascii=False)
            else:
                yield path, v
    else:
        yield prefix, d


# ==================== 数据加载 ====================

def load_reviews(run_dir: Path, model: str):
    out = []
    rdir = run_dir / "reviews"
    if not rdir.is_dir():
        return out
    subs = []
    mdir = rdir / model
    if mdir.is_dir():
        # rglob 递归：子集名可能含 "/"（如 hle 的 Biology/Medicine），
        # evalscope 会写成 reviews/<model>/hle_Biology/Medicine.jsonl 嵌套路径
        subs = sorted(mdir.rglob("*.jsonl"))
    if not subs:
        subs = sorted(p for d in rdir.iterdir() if d.is_dir()
                      for p in d.rglob("*.jsonl"))
    for p in subs:
        # ds = reviews/<model>/ 下的相对路径（去 .jsonl 后缀），保留嵌套子集名；
        # 非嵌套时与 p.stem 等价
        try:
            model_dir = rdir / p.relative_to(rdir).parts[0]
            ds = str(p.relative_to(model_dir))
        except (ValueError, IndexError):
            ds = p.name
        if ds.endswith(".jsonl"):
            ds = ds[:-len(".jsonl")]
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append((ds, json.loads(line)))
                except json.JSONDecodeError:
                    continue
    return out


def load_skipped(run_dir: Path, model: str):
    p = run_dir / "predictions" / model / "skipped_samples.jsonl"
    out = []
    if p.is_file():
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    if out:
        return out
    # 兜底：样本在「数据处理/推理」阶段失败时（如 mmmu_pro vision 格式部分样本
    # list index out of range），evalscope 只在 eval_log.log 里记
    #   ERROR: Processing item in subset='X' failed: <err>
    #   WARNING: Error ignored, continuing with next sample.
    # 不写 skipped_samples.jsonl。不补上这批题，基础题数/覆盖率会少算。
    stems = []
    mdir = run_dir / "reviews" / model
    if mdir.is_dir():
        # rglob + 相对路径：子集名可能含 "/"（如 hle 的 Biology/Medicine）
        stems = [str(q.relative_to(mdir))[:-len(".jsonl")] for q in mdir.rglob("*.jsonl")]
    log_p = run_dir / "logs" / "eval_log.log"
    if not log_p.is_file():
        return []
    try:
        lines = log_p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return parse_skipped_from_eval_log(lines, stems)


_EVAL_LOG_SKIP_PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}).*?ERROR: Processing item in subset='([^']+)' failed: ?(.*)$"
)


def parse_skipped_from_eval_log(lines, stems):
    """从 eval_log 行序列提取被 evalscope ignore_errors 跳过的样本。

    lines：可迭代的行序列（list 或 generator，COS 流式读取时是 generator）。
    stems：reviews 文件 stem 列表（如 mmmu_pro_Accounting），用于把日志里的
    裸子集名（Accounting）映射成与 cases 一致的 dataset 命名。
    """
    lines = list(lines)  # generator 物化，后续要 len()/按下标扫描

    def _stem_of(subset: str) -> str:
        for st in stems:
            if st == subset or st.endswith("_" + subset):
                return st
        return subset

    out = []
    i = 0
    n = len(lines)
    while i < n:
        m = _EVAL_LOG_SKIP_PAT.search(lines[i])
        if not m:
            i += 1
            continue
        ts, subset, err = m.group(1), m.group(2), (m.group(3) or "").strip()
        # 收集紧随其后的 traceback 行（直到下一条带时间戳的日志或固定上限）
        detail = []
        j = i + 1
        while j < n and len(detail) < 30:
            nxt = lines[j]
            if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2} - ", nxt):
                break
            if nxt.strip():
                detail.append(nxt)
            j += 1
        # 异常行（如 "IndexError: list index out of range"）并入错误信息
        err_full = err
        if detail and (not err or err.lower() in detail[-1].lower()):
            err_full = (err + " " if err else "") + detail[-1].strip()
        out.append({
            "sample_id": f"log-{len(out) + 1}",
            "subset": _stem_of(subset),
            "ts": ts,
            "error_type": "processing_error",
            "error": err_full or "Processing item failed",
            "traceback": "\n".join(detail),
        })
        i = j
    return out


def load_evalscope_reports(run_dir: Path, model: str):
    out = []
    rdir = run_dir / "reports"
    if not rdir.is_dir():
        return out
    cands = []
    mdir = rdir / model
    if mdir.is_dir():
        cands.extend(sorted(mdir.glob("*.json")))
    cands.extend(sorted(p for p in rdir.glob("*.json")))
    for p in cands:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def build_swebench_index(run_dir: Path):
    root = run_dir / "swebench_log"
    idx = {}
    if not root.is_dir():
        return idx
    for d in root.iterdir():
        if not d.is_dir():
            continue
        patch = ""
        pf = d / "patch.diff"
        if pf.is_file():
            patch = pf.read_text(encoding="utf-8", errors="replace").strip()
        test_result = None
        log_dir = d / "test_output.txt"
        if log_dir.is_file():
            tail = log_dir.read_text(encoding="utf-8", errors="replace")[-800:]
            if re.search(r"Ran \d+ tests[\s\S]*?\nOK\b", tail) or re.search(r"\nOK\s*$", tail):
                test_result = "PASS"
            elif "FAILED" in tail or "ERROR" in tail:
                test_result = "FAIL"
            else:
                test_result = "UNKNOWN"
        if patch:
            idx[patch] = {"instance_id": d.name, "test_result": test_result}
    return idx


# ==================== 指标提取 ====================

def extract_perf_calls(rec: dict):
    calls = []
    for m in rec.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        pm = m.get("perf_metrics")
        if not pm:
            continue
        calls.append({
            "latency": float(pm.get("latency", 0) or 0),
            "ttft": float(pm.get("ttft", 0) or 0),
            "input_tokens": int(pm.get("input_tokens", 0) or 0),
            "output_tokens": int(pm.get("output_tokens", 0) or 0),
            "tpot": float(pm.get("tpot", 0) or 0),
        })
    return calls


def extract_duration(rec: dict):
    at = rec.get("agent_trace") or {}
    evs = at.get("events") or []
    ts = [e.get("timestamp") for e in evs if isinstance(e.get("timestamp"), (int, float))]
    if len(ts) >= 2:
        return max(ts) - min(ts)
    lats = [float(m["perf_metrics"].get("latency", 0) or 0)
            for m in rec.get("messages", []) or []
            if isinstance(m, dict) and isinstance(m.get("perf_metrics"), dict)]
    return sum(lats) if lats else 0.0


def extract_title(rec: dict, max_len=120):
    for m in rec.get("messages", []) or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                t = re.sub(r"\s+", " ", c.strip())
                return t[:max_len] + ("…" if len(t) > max_len else "")
            if isinstance(c, list):
                for item in c:
                    if isinstance(item, dict) and item.get("text", "").strip():
                        t = re.sub(r"\s+", " ", item["text"].strip())
                        return t[:max_len] + ("…" if len(t) > max_len else "")
    return f"sample-{rec.get('index', '?')}"


def extract_score(rec: dict):
    ss = rec.get("sample_score") or {}
    sc = ss.get("score") or {}
    val = sc.get("value") or {}
    acc = None
    # 1) 常见主分键（acc/score/accuracy/main_score，覆盖 tau2/swe 等多数数据集）
    for k in ("acc", "score", "accuracy", "main_score"):
        if k in val:
            try:
                acc = float(val[k])
            except (TypeError, ValueError):
                pass
            break
    # 2) evalscope 标准 main_score_name：simple_qa=is_correct、air_bench_chat=judge_score、
    #    alpaca_eval=win_rate 等数据集主分键名各异，统一走 Score.main_score_name 取主分
    #    （ER-00110：simple_qa 三分类 value 无 acc 键 → 全量 acc=None → 有效通过率误显 0）
    if acc is None:
        msn = sc.get("main_score_name")
        if msn and msn in val:
            try:
                acc = float(val[msn])
            except (TypeError, ValueError):
                acc = None
    # 3) 兜底：与框架 Score.get_main_score 行为对齐——value 为数值 dict 时取首键
    if acc is None and isinstance(val, dict) and val:
        for v in val.values():
            try:
                acc = float(v)
                break
            except (TypeError, ValueError):
                continue
    return acc, sc.get("extracted_prediction", "") or "", sc.get("explanation", "") or ""


def build_case(rec: dict, dataset: str, swe_idx: dict):
    acc, prediction, explanation = extract_score(rec)
    calls = extract_perf_calls(rec)
    duration = extract_duration(rec)
    swe = swe_idx.get((prediction or "").strip()) if prediction else None
    ttfts = [c["ttft"] for c in calls if c["ttft"] > 0]
    in_tok = sum(c["input_tokens"] for c in calls)
    out_tok = sum(c["output_tokens"] for c in calls)
    model_time = sum(c["latency"] for c in calls)
    first_ttft = ttfts[0] if ttfts else 0.0
    avg_ttft = statistics.mean(ttfts) if ttfts else 0.0
    tps = (out_tok / model_time) if model_time > 0 else 0.0
    instance_id = (swe or {}).get("instance_id", "")
    test_result = (swe or {}).get("test_result")
    return {
        "index": rec.get("index", "?"),
        "dataset": dataset,
        "title": extract_title(rec),
        "instance_id": instance_id,
        "test_result": test_result,
        "acc": acc,
        "prediction": prediction,
        "explanation": explanation,
        "target": rec.get("target", ""),
        "patch": "",
        "messages": rec.get("messages", []) or [],
        "calls": calls,
        "call_count": len(calls),
        "duration": duration,
        "model_time": model_time,
        "tool_time": max(0.0, duration - model_time),
        "first_ttft": first_ttft,
        "avg_ttft": avg_ttft,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "tps": tps,
        "has_perf": bool(calls),
    }


# ==================== 思维链流水渲染 ====================

def render_message_blocks(messages):
    parts = []
    step = 0
    msgs = [m for m in messages if isinstance(m, dict)]
    if MAX_FLOW_MESSAGES and len(msgs) > MAX_FLOW_MESSAGES:
        keep_tail = max(10, MAX_FLOW_MESSAGES // 3)
        head = msgs[:MAX_FLOW_MESSAGES - keep_tail]
        tail = msgs[-keep_tail:]
        omitted = len(msgs) - len(head) - len(tail)
        msgs = head + [{"role": "__omitted__", "count": omitted}] + tail
    for m in msgs:
        role = m.get("role", "?")
        if role == "__omitted__":
            parts.append(
                f'<div class="msg other"><div class="role-tag">省略</div>'
                f'<pre>… 中间省略 {m["count"]} 条消息（紧凑模式仅展示首尾）…</pre></div>')
            continue
        content = m.get("content")
        if role == "system":
            text, clipped = clip(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
            parts.append(
                f'<details class="msg system"><summary>系统提示（点击展开）</summary>'
                f'<pre>{escape(text)}{"…(截断)" if clipped else ""}</pre></details>')
            continue
        if role == "user":
            text, clipped = clip(content if isinstance(content, str) else _content_list_text(content))
            parts.append(
                f'<div class="msg user"><div class="role-tag">用户</div>'
                f'<pre>{escape(text)}{"…(截断)" if clipped else ""}</pre></div>')
            continue
        if role == "assistant":
            step += 1
            pm = m.get("perf_metrics") or {}
            pm_txt = ""
            if pm:
                pm_txt = (f'<span class="perf">ttft {fmt(float(pm.get("ttft", 0) or 0))}s · '
                          f'耗时 {fmt(float(pm.get("latency", 0) or 0))}s · '
                          f'输入 {fmt_tok(pm.get("input_tokens", 0) or 0)} · '
                          f'输出 {fmt_tok(pm.get("output_tokens", 0) or 0)}</span>')
            parts.append(f'<div class="msg assistant"><div class="role-tag">助手 · 第 {step} 步{pm_txt}</div>')
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "reasoning":
                        rk = item.get("reasoning", "")
                        rtok = item.get("reasoning_tokens")
                        text, clipped = clip(rk)
                        label = f"思考过程（{rtok} tokens）" if rtok else "思考过程"
                        parts.append(
                            f'<details class="reasoning"><summary>{escape(label)}</summary>'
                            f'<pre>{escape(text)}{"…(截断)" if clipped else ""}</pre></details>')
                    elif item.get("type") == "text":
                        text, clipped = clip(item.get("text", ""))
                        if text.strip():
                            parts.append(f'<pre class="answer">{escape(text)}{"…(截断)" if clipped else ""}</pre>')
            elif isinstance(content, str) and content.strip():
                text, clipped = clip(content)
                parts.append(f'<pre class="answer">{escape(text)}{"…(截断)" if clipped else ""}</pre>')
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or "tool"
                args = fn.get("arguments") or tc.get("arguments") or ""
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                args, clipped = clip(args, 800)
                parts.append(
                    f'<div class="tool-call">调用工具 <code>{escape(str(name))}</code>'
                    f'<pre>{escape(args)}{"…(截断)" if clipped else ""}</pre></div>')
            parts.append('</div>')
            continue
        if role == "tool":
            text, clipped = clip(content if isinstance(content, str) else _content_list_text(content), 1500)
            parts.append(
                f'<details class="msg tool"><summary>工具返回（点击展开）</summary>'
                f'<pre>{escape(text)}{"…(截断)" if clipped else ""}</pre></details>')
            continue
        text, clipped = clip(content if isinstance(content, str) else _content_list_text(content), 1000)
        if text.strip():
            parts.append(f'<div class="msg other"><div class="role-tag">{escape(str(role))}</div>'
                         f'<pre>{escape(text)}{"…(截断)" if clipped else ""}</pre></div>')
    return "\n".join(parts)


def render_skip_trajectory(traj):
    parts = []
    msgs = [m for m in (traj or []) if isinstance(m, dict)]
    if MAX_FLOW_MESSAGES and len(msgs) > MAX_FLOW_MESSAGES:
        keep_tail = max(10, MAX_FLOW_MESSAGES // 3)
        head = msgs[:MAX_FLOW_MESSAGES - keep_tail]
        tail = msgs[-keep_tail:]
        omitted = len(msgs) - len(head) - len(tail)
        msgs = head + [{"role": "__omitted__", "count": omitted}] + tail
    step = 0
    for m in msgs:
        role = str(m.get("role", "")).lower().replace("role.", "")
        if role == "__omitted__":
            parts.append(f'<div class="msg other"><div class="role-tag">省略</div>'
                         f'<pre>… 中间省略 {m["count"]} 条消息 …</pre></div>')
            continue
        content = m.get("content")
        ts = m.get("timestamp")
        ts_txt = f' · {escape(str(ts))}' if ts else ''
        if role == "user":
            text = content if isinstance(content, str) else _content_list_text(content)
            t, cl = clip(text, 1500)
            parts.append(f'<div class="msg user"><div class="role-tag">用户{ts_txt}</div>'
                         f'<pre>{escape(t)}{"…(截断)" if cl else ""}</pre></div>')
        elif role == "assistant":
            step += 1
            parts.append(f'<div class="msg assistant"><div class="role-tag">助手 · 第 {step} 步{ts_txt}</div>')
            rc = m.get("reasoning_content")
            if rc:
                t, cl = clip(rc)
                parts.append(f'<details class="reasoning"><summary>思考过程</summary>'
                             f'<pre>{escape(t)}{"…(截断)" if cl else ""}</pre></details>')
            if isinstance(content, str) and content.strip():
                t, cl = clip(content)
                parts.append(f'<pre class="answer">{escape(t)}{"…(截断)" if cl else ""}</pre>')
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or "tool"
                args = fn.get("arguments") or tc.get("arguments") or ""
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                a, cl = clip(args, 800)
                parts.append(f'<div class="tool-call">调用工具 <code>{escape(str(name))}</code>'
                             f'<pre>{escape(a)}{"…(截断)" if cl else ""}</pre></div>')
            parts.append('</div>')
        elif role == "tool":
            text = content if isinstance(content, str) else _content_list_text(content)
            t, cl = clip(text, 1500)
            parts.append(f'<details class="msg tool"><summary>工具返回{ts_txt}</summary>'
                         f'<pre>{escape(t)}{"…(截断)" if cl else ""}</pre></details>')
        else:
            text = content if isinstance(content, str) else _content_list_text(content)
            t, cl = clip(text, 1000)
            if t.strip():
                parts.append(f'<div class="msg other"><div class="role-tag">{escape(role)}{ts_txt}</div>'
                             f'<pre>{escape(t)}{"…(截断)" if cl else ""}</pre></div>')
    return "\n".join(parts)


def render_skip_body(s):
    sid = s.get("sample_id", "?")
    subset = s.get("subset", "")
    ts = s.get("ts", "")
    etype = s.get("error_type", "")
    err = (s.get("error") or "").strip()
    err_short = err.split("\n")[0][:160]
    tb = s.get("traceback") or ""
    desc = ""
    prompt = s.get("prompt")
    if isinstance(prompt, list) and prompt:
        desc = (prompt[0].get("content", "") if isinstance(prompt[0], dict) else str(prompt[0])) or ""
    meta = s.get("metadata") or {}
    if not desc and isinstance(meta, dict):
        d = meta.get("description")
        if isinstance(d, dict):
            desc = d.get("purpose", "") or ""
    d, cl = clip(str(desc), 500)
    traj = s.get("partial_trajectory")
    n_traj = s.get("partial_trajectory_count") or (len(traj) if isinstance(traj, list) else 0)
    body = []
    body.append('<div class="task-meta">'
                f'<div><span class="k">数据集:</span> <span class="v">{escape(str(subset))}</span></div>'
                f'<div><span class="k">sample_id:</span> <span class="v">{escape(str(sid))}</span></div>'
                f'<div><span class="k">时间:</span> <span class="v">{escape(str(ts))}</span></div>'
                f'<div><span class="k">出错前轨迹:</span> <span class="v">{n_traj} 条</span></div>'
                '</div>')
    body.append(f'<div class="score-box"><div><div class="k">题目描述</div>'
                f'<div class="mono">{escape(d)}{"…" if cl else ""}</div></div></div>')
    body.append('<div class="score-box">'
                f'<div><div class="k">错误类型</div><div class="mono">{escape(str(etype))}</div></div>'
                f'<div><div class="k">错误信息</div><div class="mono">{escape(err_short)}</div></div>'
                '</div>')
    if tb:
        t, cl2 = clip(tb, 4000)
        body.append(f'<details class="msg tool"><summary>完整 Traceback</summary>'
                    f'<pre class="patch-pre">{escape(t)}{"…(截断)" if cl2 else ""}</pre></details>')
    if traj:
        body.append('<h3>出错前交互轨迹</h3>')
        body.append(render_skip_trajectory(traj))
    return " ".join(body), sid, subset, etype, err_short


# ==================== 模版各章节渲染 ====================

def _dataset_label(name):
    m = {"airline": "航空 Airline", "retail": "零售 Retail", "telecom": "通信 Telecom"}
    return m.get(name, name)


def _subset_from_dataset(ds):
    for k in ("airline", "retail", "telecom"):
        if k in ds:
            return k
    return ds


def render_hero(model, reps, cases, skipped, cfg):
    rep0 = reps[0] if reps else {}
    ds_pretty = rep0.get("dataset_pretty_name") or rep0.get("dataset_name") or "评测"
    ds_name = rep0.get("dataset_name") or ""
    # 基础题数：总执行样本 / repeats
    repeats = (cfg or {}).get("repeats") or 1
    base_n = (len(cases) + len(skipped)) // repeats if repeats else len(cases)
    total_runs = len(cases) + len(skipped)
    batch = (cfg or {}).get("eval_batch_size") or (cfg or {}).get("generation_config", {}) or {}
    batch_size = (cfg.get("eval_batch_size") if cfg else 0) or 0
    judge_model = ""
    jma = (cfg or {}).get("judge_model_args") or {}
    if jma.get("model_id"):
        judge_model = jma["model_id"]
    # 数据集域描述
    subs = sorted({_subset_from_dataset(c["dataset"]) for c in cases} | {s.get("subset", "") for s in skipped})
    subs = [s for s in subs if s]
    domain_txt = " / ".join(_dataset_label(s) for s in subs) if subs else ds_pretty
    subtitle = (f"被测模型 {model} 在 {ds_pretty} {'、'.join(_dataset_label(s) for s in subs) if subs else ''} "
                f"任务上的端到端能力、稳定性与交互性能评测。")
    tags = []
    tags.append(f'<div class="hero-tag"><span class="hero-tag-label">数据集名称</span>'
                f'<span class="hero-tag-value">{escape(str(ds_pretty))} · {escape(domain_txt)}</span></div>')
    tags.append(f'<div class="hero-tag"><span class="hero-tag-label">基础题数</span>'
                f'<span class="hero-tag-value">{base_n} 题</span></div>')
    tags.append(f'<div class="hero-tag"><span class="hero-tag-label">执行轮次</span>'
                f'<span class="hero-tag-value">每题 {repeats} 轮 · 共 {total_runs:,} 次</span></div>')
    tags.append(f'<div class="hero-tag"><span class="hero-tag-label">执行并发数</span>'
                f'<span class="hero-tag-value">{batch_size or "—"} 路并发 · eval_batch_size={batch_size or "—"}</span></div>')
    tags.append(f'<div class="hero-tag"><span class="hero-tag-label">裁判策略</span>'
                f'<span class="hero-tag-value">Auto · {escape(judge_model or "—")} · NL 断言</span></div>')
    return (
        f'<header class="report-hero">'
        f'<div class="hero-topline"><div>'
        f'<div class="hero-kicker">MODEL EVALUATION REPORT · {escape(str(ds_name).upper())}</div>'
        f'<h1><span class="hero-model">{escape(model)}</span> × <span class="hero-dataset">{escape(str(ds_pretty))}</span> 模型评测报告</h1>'
        f'<p class="hero-subtitle">{escape(subtitle)}</p></div>'
        f'<button class="hero-action" type="button" onclick="window.print()">打印 / 导出 PDF</button></div>'
        f'<div class="hero-tags" aria-label="评测基本信息">{"".join(tags)}</div></header>'
    )


def _is_binary_cases(cases) -> bool:
    """分数是否为 0/1 二值指标。

    二值指标（acc∈{0,1}，如 swe/选择题）展示「通过率」（满分占比）；
    连续指标（如 longbench_score 为 0~1 的 rouge/f1）几乎无满分样本，
    通过率恒≈0 且无意义，改展示「平均分」。
    """
    accs = [c["acc"] for c in cases if c["acc"] is not None]
    return bool(accs) and all(a in (0, 1) for a in accs)


def render_conclusion(model, cases, skipped, reps, perf_cases):
    n = len(cases)
    scored = [c for c in cases if c["acc"] is not None]
    binary = _is_binary_cases(cases)
    if binary:
        passed = sum(1 for c in scored if c["acc"] >= 1)
        pass_rate = (passed / len(scored) * 100) if scored else 0.0
        score_label, score_value = '有效题通过率', f'{pass_rate:.2f}%'
        score_note = f'{passed:,} / {len(scored):,} 题'
    else:
        avg_score = statistics.mean(c["acc"] for c in scored) * 100 if scored else 0.0
        score_label, score_value = '平均分', f'{avg_score:.2f}'
        score_note = f'{len(scored):,} 题 · 连续指标（0~100）'
    total_runs = n + len(skipped)
    coverage = (n / total_runs * 100) if total_runs else 0.0
    n_skip = len(skipped)

    def pair(vals, unit):
        vals = [v for v in vals if v]
        if not vals:
            return '<span class="pair-value">—</span>'
        return (f'<span class="pair-value">{fmt(pct(vals, 0.5))}{unit}</span>')
    # KPI block 1
    blk1 = (
        '<div class="kpi-block"><h3 class="kpi-block-title">效果与覆盖 <span class="meta">任务成功率 · 完整度</span></h3>'
        '<div class="kpi-mini-grid">'
        f'<div class="kpi-card primary"><div class="kpi-label">{score_label}</div><div class="kpi-value">{score_value}</div>'
        f'<div class="kpi-note">{score_note}</div></div>'
        f'<div class="kpi-card"><div class="kpi-label">执行覆盖率</div><div class="kpi-value good">{coverage:.2f}%</div>'
        f'<div class="kpi-note">{n:,} / {total_runs:,} · <a class="kpi-link-alert" href="#skipped">跳过 {n_skip}</a></div></div>'
        '</div></div>'
    )
    # KPI block 2 体验性能
    first_ttfts = [c["first_ttft"] for c in perf_cases if c["first_ttft"] > 0]
    tpots = [c["calls"][0]["tpot"] * 1000 for c in perf_cases if c["calls"] and c["calls"][0]["tpot"] > 0]
    durs = [c["duration"] / 60 for c in perf_cases if c["duration"] > 0]
    def pp(title, vals, unit):
        if not vals:
            return f'<div class="kpi-pair"><div class="pair-title">{title}</div><div class="pair-row"><span class="pair-label">P50</span><span class="pair-value">—</span></div><div class="pair-row"><span class="pair-label">P95</span><span class="pair-value">—</span></div></div>'
        return (f'<div class="kpi-pair"><div class="pair-title">{title}</div>'
                f'<div class="pair-row"><span class="pair-label">P50</span><span class="pair-value">{fmt(pct(vals,0.5))}{unit}</span></div>'
                f'<div class="pair-row"><span class="pair-label">P95</span><span class="pair-value">{fmt(pct(vals,0.95))}{unit}</span></div></div>')
    blk2 = (
        '<div class="kpi-block"><h3 class="kpi-block-title">体验与性能 <span class="meta">首字时延 · 生成时延 · 单题耗时</span></h3>'
        '<div class="kpi-mini-grid pairs">'
        + pp("TTFT 首字时延", first_ttfts, "s") + pp("TPOT 生成时延", tpots, "ms") + pp("单题耗时", durs, "m")
        + '</div></div>'
    )
    # KPI block 3 资源
    in_total = sum(c["input_tokens"] for c in perf_cases)
    out_total = sum(c["output_tokens"] for c in perf_cases)
    blk3 = (
        '<div class="kpi-block"><h3 class="kpi-block-title">资源消耗 <span class="meta">总输入 · 总输出 Token</span></h3>'
        '<div class="kpi-mini-grid">'
        f'<div class="kpi-card"><div class="kpi-label">累计输入 Token</div><div class="kpi-value">{in_total:,}</div>'
        f'<div class="kpi-note">约 {in_total/1e8:.2f} 亿 tokens</div></div>'
        f'<div class="kpi-card"><div class="kpi-label">累计输出 Token</div><div class="kpi-value">{out_total:,}</div>'
        f'<div class="kpi-note">约 {out_total/1e4:.1f} 万 tokens</div></div>'
        '</div></div>'
    )
    kpi = f'<div class="kpi-group">{blk1}{blk2}{blk3}</div>'

    # 跳过摘要
    skip_summary = ""
    if skipped:
        from collections import Counter
        etypes = Counter(s.get("error_type", "") for s in skipped)
        common_etype = etypes.most_common(1)[0][0] if etypes else ""
        common_err = ""
        errs = [(s.get("error") or "").split("\n")[0] for s in skipped if s.get("error")]
        if errs:
            common_err = Counter(errs).most_common(1)[0][0][:160]
        # 域分布
        dom_cnt = Counter(s.get("subset", "") for s in skipped)
        all_domains = sorted({_subset_from_dataset(c["dataset"]) for c in cases})
        dist_rows = "".join(
            f'<div class="skipped-distribution"><span class="skipped-domain">{escape(_dataset_label(d))}</span>'
            f'<span class="skipped-count">{dom_cnt.get(d, 0)} 题</span></div>'
            for d in all_domains)
        # 触发步数：从 partial_trajectory_count 推断
        steps = [s.get("partial_step_count") for s in skipped if s.get("partial_step_count")]
        step_txt = f"第 {min(steps)} 步" if steps else "执行过程中"
        root_tag = "被测模型的问题" if common_etype in ("ValueError",) else "框架/环境问题"
        root_text = (f"模型在特定条件下返回了空响应，框架正确地捕获了这个异常。" if common_etype == "ValueError"
                     else f"执行过程中出现 {common_etype or '异常'}，被框架捕获。")
        cause = (f"模型在 {step_txt} 返回了空 AssistantMessage（既无文本内容也未发起工具调用），"
                 f"被框架校验为 <code>{escape(common_etype)}: {escape(common_err)}</code>。"
                 f"该异常在框架最终验证阶段抛出，导致评测无法继续评估任务结果。") if common_etype == "ValueError" else (
                 f"执行过程中出现 <code>{escape(common_etype or '异常')}</code>：{escape(common_err)}。"
                 f"该异常导致评测无法继续评估任务结果。")
        skip_summary = (
            '<section class="skipped-summary" aria-label="异常跳过摘要">'
            '<header class="skipped-summary-head">'
            '<div class="skipped-summary-title"><span class="skipped-icon">!</span>异常跳过摘要</div>'
            f'<a class="skipped-summary-link" href="#skipped">查看完整 {n_skip} 道跳过题 →</a></header>'
            '<div class="skipped-summary-grid">'
            f'<div class="skipped-stat"><div class="k">受影响样本</div><div class="v">{n_skip} / {total_runs:,}（{n_skip/total_runs*100:.2f}%）</div></div>'
            f'<div class="skipped-stat"><div class="k">失败环节</div><div class="v">{step_txt}：模型输出校验</div></div>'
            '<div class="skipped-stat"><div class="k">是否计入有效通过率</div><div class="v">否</div></div></div>'
            '<div class="skipped-root"><div class="skipped-root-label">核心根因</div>'
            f'<div class="skipped-root-tag">{escape(root_tag)}</div>'
            f'<div class="skipped-root-text">{escape(root_text)}</div></div>'
            '<details class="skipped-details"><summary>查看详情</summary><div class="skipped-details-body">'
            f'<h4 class="skipped-cause-title">{n_skip} 道题的共同异常原因</h4>'
            f'<p class="skipped-cause">{cause}</p>'
            '<h4 class="skipped-cause-title">样本覆盖</h4>' + dist_rows +
            '<h4 class="skipped-cause-title">触发分布</h4>'
            '<p class="skipped-cause">出现时机均为助手回应阶段；结合生成配置，可能是思考链路输出被截断或生成终止符被消耗。</p>'
            '<h4 class="skipped-cause-title">修复建议</h4><ol class="skipped-actions">'
            '<li>复现任意一个 SKIP 样本，确认 AssistantMessage 在对应轮次是否被空字符串或纯结束符触发。</li>'
            '<li>在被测模型侧开启 enable_thinking 时，确保思考内容不会与最终回复竞争 message 输出。</li>'
            '<li>修复后，使用同配置重新执行 SKIP 样本，并在下一份报告中将其纳入有效题统计。</li>'
            '</ol></div></details></section>'
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    domains = sorted({_subset_from_dataset(c["dataset"]) for c in cases})
    meta = (f'<div class="report-meta"><span>被测模型 <b>{escape(model)}</b></span>'
            f'<span>测试范围 <b>{escape(" / ".join(_dataset_label(d) for d in domains))}</b></span>'
            f'<span>报告生成 <b>{now}</b></span></div>')
    return (
        '<h2 id="overview">一、核心结论</h2>' + meta + kpi + skip_summary
    )


def render_quality(reps):
    if not reps:
        return ""
    parts = ['<h2 id="quality">二、效果与稳定性</h2>']
    r = reps[0]
    pass1 = next((m.get("score") for m in (r.get("metrics") or []) if m.get("name") == "mean_acc_pass^1"), None)
    pass3 = next((m.get("score") for m in (r.get("metrics") or []) if m.get("name") == "mean_acc_pass^3"), None)
    intro = (f"先看模型能否完成任务，再看同类任务在重复执行中是否持续成功。"
             f"单次通过率 {pass1*100:.2f}%，" if pass1 else "先看模型能否完成任务，再看同类任务在重复执行中是否持续成功。")
    if pass3 is not None:
        intro += f"但 pass³ 为 {pass3*100:.2f}%，提示多次重复执行时仍存在稳定性损耗。"
    parts.append(f'<p class="section-intro">{escape(intro)}</p>')
    parts.append('<h3>综合得分与重复测试稳定性</h3>')
    metrics = r.get("metrics") or []
    if metrics:
        parts.append('<table class="metric-table"><thead><tr><th>指标</th><th>得分</th><th>Macro 得分</th><th>题数</th></tr></thead><tbody>')
        for m in metrics:
            ms = m.get("score")
            mm = m.get("macro_score", ms)
            parts.append(f'<tr><td class="metric-name">{escape(str(m.get("name", "")))}</td>'
                         f'<td>{ms:.4f}</td><td>{mm:.4f}</td><td>{m.get("num", "—")}</td></tr>')
        parts.append('</tbody></table>')
    subs = []
    for m in metrics:
        for cat in m.get("categories") or []:
            if cat.get("subsets"):
                subs = cat["subsets"]
                break
        if subs:
            break
    if subs:
        parts.append('<table class="metric-table"><thead><tr><th>子数据集</th><th>得分</th><th>题数</th></tr></thead><tbody>')
        for s in subs:
            sv = s.get("score")
            pctv = sv * 100 if isinstance(sv, (int, float)) else 0
            risk = " risk" if pctv < 80 else (" good" if pctv >= 90 else "")
            warn_cls = ' class="warn"' if pctv < 80 else ''
            parts.append(
                f'<tr><td class="metric-name domain-cell"><div class="domain-name"><span>{escape(_dataset_label(s.get("name", "")))}</span>'
                f'<span>{pctv:.2f}%</span></div><div class="domain-track"><div class="domain-fill{risk}" style="width:{pctv:.2f}%"></div></div></td>'
                f'<td{warn_cls}>{sv:.4f}</td><td>{s.get("num", "—")}</td></tr>')
        parts.append('</tbody></table>')
    return "\n".join(parts)


def render_performance(perf_cases):
    if not perf_cases:
        return ""
    n = len(perf_cases)

    def row(primary, secondary, unit, vals, digits=2):
        vals = [v for v in vals if v]
        if not vals:
            return f'<tr><td><div class="metric-primary">{primary}</div><div class="metric-secondary">{secondary}</div></td><td><span class="unit-pill">{unit}</span></td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'
        return (f'<tr><td><div class="metric-primary">{primary}</div><div class="metric-secondary">{secondary}</div></td>'
                f'<td><span class="unit-pill">{unit}</span></td>'
                f'<td>{fmt(statistics.mean(vals), digits)}</td><td>{fmt(pct(vals, 0.5), digits)}</td>'
                f'<td>{fmt(pct(vals, 0.9), digits)}</td><td>{fmt(pct(vals, 0.95), digits)}</td></tr>')

    rows = []
    rows.append(row("首字 TTFT", "每个 case 第一次模型调用的首 Token 时延", "s",
                     [c["first_ttft"] for c in perf_cases if c["first_ttft"] > 0]))
    rows.append(row("所有模型调用 TTFT", "每个 case 内全部模型调用 TTFT 的平均值", "s",
                     [c["avg_ttft"] for c in perf_cases if c["avg_ttft"] > 0]))
    rows.append(row("输入 Token 数", "每个 case 累计输入 Token", "Token",
                     [c["input_tokens"] for c in perf_cases], 0))
    rows.append(row("输出 Token 数", "每个 case 累计输出 Token", "Token",
                     [c["output_tokens"] for c in perf_cases], 0))
    rows.append(row("TPOT", "每个 case 的单 Token 平均生成时延", "ms",
                     [c["calls"][0]["tpot"] * 1000 for c in perf_cases if c["calls"] and c["calls"][0]["tpot"] > 0]))
    rows.append(row("调用模型次数", "每个 case 发起的模型调用总数", "次",
                     [c["call_count"] for c in perf_cases], 1))
    rows.append(row("题目耗时", "每个 case 端到端总耗时", "min",
                     [c["duration"] / 60 for c in perf_cases if c["duration"] > 0]))
    return (
        '<h2 id="performance">三、用户体验与性能</h2>'
        f'<p class="section-intro">全部指标均按 {n:,} 个有效 case 统计。Avg 反映整体水平，P50 代表典型体验，P90/P95 用于观察长尾。</p>'
        '<div class="performance-panel"><div class="performance-head"><div><h3>Case 维度性能分位</h3>'
        '<p>首字响应、全部模型调用、Token 消耗、生成时延与调用规模</p></div>'
        f'<span class="performance-sample">有效样本 {n:,} cases</span></div>'
        '<div class="performance-table-wrap"><table class="metric-table performance-table">'
        '<thead><tr><th>指标</th><th>单位</th><th>Avg</th><th>P50</th><th>P90</th><th>P95</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div></div>'
    )


def render_config(cfg, reps):
    if not cfg:
        return ""
    r = reps[0] if reps else {}
    ds_pretty = r.get("dataset_pretty_name") or r.get("dataset_name") or ""
    model = cfg.get("model_id") or cfg.get("model") or ""
    api_url = cfg.get("api_url") or ""
    gc = cfg.get("generation_config") or {}
    jma = cfg.get("judge_model_args") or {}
    judge_model = jma.get("model_id") or ""
    judge_api = jma.get("api_url") or ""
    jgc = jma.get("generation_config") or {}
    # 被测模型 tags
    subj_tags = []
    if cfg.get("eval_type"): subj_tags.append(cfg["eval_type"])
    if gc.get("stream"): subj_tags.append("流式输出")
    if gc.get("enable_thinking"): subj_tags.append("Thinking")
    subj_tags_html = "".join(f'<span class="config-tag">{escape(t)}</span>' for t in subj_tags)
    # 裁判 tags
    judge_tags = ["Auto Judge", "NL Assertions", f'{cfg.get("repeats", 1)} Rounds']
    judge_tags_html = "".join(f'<span class="config-tag">{escape(t)}</span>' for t in judge_tags)

    def kv_table(rows, highlight_keys=()):
        out = ['<table class="metric-table"><tbody>']
        for k, v in rows:
            cls = " config-row-highlight" if k in highlight_keys else ""
            out.append(f'<tr{cls}><td class="metric-name mono">{escape(str(k))}</td>'
                       f'<td class="mono{" config-value-strong" if k in highlight_keys else ""}">{escape(str(_mask_secret(k, v)))}</td></tr>')
        out.append('</tbody></table>')
        return "".join(out)

    # 被测模型信息
    subj_model_rows = [("model_id", model), ("eval_type", cfg.get("eval_type", "")), ("api_url", api_url)]
    # 生成配置（按表单顺序）
    gc_order = ["temperature", "max_tokens", "top_p", "timeout", "stream", "enable_thinking"]
    gen_rows = [(k, gc[k]) for k in gc_order if k in gc and gc[k] is not None]
    gen_rows += [(k, v) for k, v in _flatten(gc) if k not in gc_order]
    gen_rows += [("执行轮次 repeats", cfg.get("repeats", "")),
                 ("执行并发数 eval_batch_size", cfg.get("eval_batch_size", "")),
                 ("随机种子 seed", cfg.get("seed", ""))]
    # 裁判模型
    judge_model_rows = [("model_id", judge_model), ("api_url", judge_api),
                        ("temperature", jgc.get("temperature", "")), ("max_tokens", jgc.get("max_tokens", ""))]
    # 评测与执行
    extra = {}
    for ds, ds_cfg in (cfg.get("dataset_args") or {}).items():
        if isinstance(ds_cfg, dict):
            ep = ds_cfg.get("extra_params") or {}
            for k, v in _flatten(ep):
                extra[k] = v
    eval_rows = [("datasets", cfg.get("datasets", "")), ("repeats", cfg.get("repeats", "")),
                 ("seed", cfg.get("seed", "")), ("eval_batch_size", cfg.get("eval_batch_size", "")),
                 ("eval_backend", cfg.get("eval_backend", "")), ("judge_strategy", cfg.get("judge_strategy", "")),
                 ("use_sandbox", cfg.get("use_sandbox", ""))]
    for k in ("evaluation_type", "user_model", "judge_model"):
        if k in extra:
            eval_rows.append((k, extra[k]))

    hl = ("执行轮次 repeats", "执行并发数 eval_batch_size", "随机种子 seed")
    return (
        '<h2 id="config">四、模型与评测配置</h2>'
        '<p class="section-intro">以下配置用于复现本次结果。所有参数集中在被测模型与裁判模型两大类，便于横向对比时定位差异。</p>'
        '<div class="config-grid">'
        '<article class="config-block subject" data-config-role="subject">'
        '<header class="config-header"><div class="config-icon" aria-hidden="true">M</div>'
        '<div class="config-heading"><div class="config-eyebrow">MODEL UNDER TEST</div>'
        f'<div class="config-title">{escape(model)}</div>'
        '<div class="config-sub">被测模型配置 · 模型接入信息与推理生成参数</div>'
        f'<div class="config-tags">{subj_tags_html}</div></div></header>'
        '<div class="config-body">'
        f'<details class="config-section" open><summary>模型信息 <span class="config-section-count">{len(subj_model_rows)} 项</span></summary>{kv_table(subj_model_rows)}</details>'
        f'<details class="config-section" open><summary>生成配置 <span class="config-section-count">{len(gen_rows)} 项</span></summary>{kv_table(gen_rows, hl)}</details>'
        '</div></article>'
        '<article class="config-block judge" data-config-role="judge">'
        '<header class="config-header"><div class="config-icon" aria-hidden="true">J</div>'
        '<div class="config-heading"><div class="config-eyebrow">JUDGE &amp; EVALUATION</div>'
        f'<div class="config-title">{escape(judge_model or "—")}</div>'
        '<div class="config-sub">裁判模型配置 · 判定模型、评测策略与执行控制</div>'
        f'<div class="config-tags">{judge_tags_html}</div></div></header>'
        '<div class="config-body">'
        f'<details class="config-section" open><summary>裁判模型 <span class="config-section-count">{len(judge_model_rows)} 项</span></summary>{kv_table(judge_model_rows)}</details>'
        f'<details class="config-section" open><summary>评测与执行 <span class="config-section-count">{len(eval_rows)} 项</span></summary>{kv_table(eval_rows)}</details>'
        '</div></article></div>'
    )


def render_cases(cases, skipped, reps):
    n_base = (len(cases) + len(skipped)) // reps if reps else len(cases)
    has_perf = any(c["has_perf"] for c in cases)
    binary = _is_binary_cases(cases)
    # 按基础题聚合统计含失败/含异常题数（前端 JS 也会聚合，这里给按钮计数）
    n_skip_group = len(skipped)
    # 业务域下拉
    domains = sorted({_subset_from_dataset(c["dataset"]) for c in cases})
    opts = "".join(f'<option value="{escape(d)}">{escape(_dataset_label(d))}</option>' for d in domains)
    total_runs = len(cases) + len(skipped)
    # 连续指标无 PASS/FAIL 概念，隐藏通过/失败筛选按钮（避免误导读空）
    result_filter_btns = ""
    if binary:
        result_filter_btns = (
            f'<button class="filter-btn result-filter" onclick="filterResult(\'pass\',this)">{reps}轮全通过</button>'
            f'<button class="filter-btn result-filter" onclick="filterResult(\'fail\',this)">含失败</button>'
        )
    if reps and reps > 1:
        intro = (f'按 {n_base} 道基础题聚合展示，每道题包含 {reps} 轮独立执行，默认收起。'
                 '先展开题目查看各轮执行，再点击单轮查看评分、模型答案、对话、工具调用和性能证据。')
        col2 = '题目名称（点击展开各轮）'
    else:
        intro = (f'共 {n_base} 道题，点击题目行展开查看评分、模型答案、对话、工具调用和性能证据。')
        col2 = '题目名称（点击展开详情）'
    head = (
        '<h2 id="cases">五、逐题结果与评测证据</h2>'
        f'<p class="section-intro">{intro}</p>'
        '<div class="toolbar issue-toolbar">'
        '<input id="issueSearch" class="search-box" type="search" placeholder="搜索题号或题目内容…" aria-label="搜索题目" oninput="filterIssues()">'
        f'<select id="datasetFilter" aria-label="筛选业务域" onchange="filterDataset()"><option value="all">全部业务域</option>{opts}</select>'
        f'<button class="filter-btn result-filter active" onclick="filterResult(\'all\',this)">全部 {n_base} 题</button>'
        f'{result_filter_btns}'
        f'<button class="filter-btn result-filter" onclick="filterResult(\'skip\',this)">含异常 {n_skip_group}</button>'
        f'<button type="button" onclick="collapseIssues()">全部收起</button>'
        f'<span id="issueCount" class="issue-count">显示 {n_base:,} / {n_base:,} 道题 · {total_runs:,} 轮</span>'
        '</div>'
    )
    base_cols = ["题号", col2, "执行结果"]
    perf_cols = ["平均调用模型次数", "平均TTFT", "平均输入token数", "平均输出token数", "平均T/s", "平均耗时"]
    cols = base_cols + perf_cols if has_perf else base_cols
    head += f'<table class="issue-summary" data-reps="{reps}"><thead><tr>' + "".join(
        f'<th class="num">{c}</th>' if i >= 2 else f'<th>{c}</th>' for i, c in enumerate(cols)) + '</tr></thead><tbody>'
    parts = [head]
    for i, c in enumerate(cases):
        title = c["instance_id"] or c["title"]
        if binary:
            rflag = "pass" if (c["acc"] is not None and c["acc"] >= 1) else "fail"
            badge = '<span class="badge pass">PASS</span>' if rflag == "pass" else '<span class="badge fail">FAIL</span>'
        else:
            # 连续指标：展示实际得分（0~100），不做通过/失败二分
            rflag = "score" if c["acc"] is not None else "skip"
            badge = (f'<span class="badge score">{c["acc"] * 100:.1f}</span>' if c["acc"] is not None
                     else '<span class="badge skip">—</span>')
        row = (f'<tr class="issue-row" data-result="{rflag}" data-dataset="{escape(_subset_from_dataset(c["dataset"]))}" onclick="toggleIssue({i})">'
               f'<td class="num">{c["index"]}</td>'
               f'<td><div class="case-name" title="{escape(title)}"><span class="caret" aria-hidden="true">▶</span>'
               f'<span class="case-title">{escape(title)}</span></div></td>'
               f'<td class="num">{badge}</td>')
        if has_perf:
            row += (f'<td class="num">{c["call_count"]}</td>'
                    f'<td class="num">{fmt(c["first_ttft"])}s</td>'
                    f'<td class="num">{fmt_tok(c["input_tokens"])}</td>'
                    f'<td class="num">{fmt_tok(c["output_tokens"])}</td>'
                    f'<td class="num">{fmt(c["tps"], 1)}</td>'
                    f'<td class="num">{fmt_dur(c["duration"])}</td>')
        parts.append(row + '</tr>')
        # 详情
        detail = []
        meta_bits = [f'数据集 <code>{escape(_dataset_label(_subset_from_dataset(c["dataset"])))}</code>']
        if c["acc"] is not None:
            meta_bits.append(f'得分 <b>{c["acc"]:.2f}</b>')
        if c["test_result"]:
            meta_bits.append(f'测试结果 {escape(str(c["test_result"]))}')
        detail.append('<div class="meta">' + ' · '.join(meta_bits) + '</div>')
        if c["target"] or c["prediction"]:
            detail.append('<div class="score-box">')
            if c["target"]:
                t, cl = clip(str(c["target"]), 500)
                detail.append(f'<div><div class="k">标准答案</div><div class="mono">{escape(t)}{"…" if cl else ""}</div></div>')
            if c["prediction"]:
                p, cl = clip(str(c["prediction"]), 500)
                detail.append(f'<div><div class="k">模型答案</div><div class="mono">{escape(p)}{"…" if cl else ""}</div></div>')
            if c["explanation"]:
                e, cl = clip(str(c["explanation"]), 300)
                detail.append(f'<div><div class="k">评分说明</div><div class="mono">{escape(e)}{"…" if cl else ""}</div></div>')
            detail.append('</div>')
        detail.append(render_message_blocks(c["messages"]))
        if c["patch"]:
            p, cl = clip(c["patch"], 3000)
            detail.append(f'<h3>Patch</h3><div class="patch-pre">{escape(p)}{"…(截断)" if cl else ""}</div>')
        colspan = 9 if has_perf else 3
        parts.append(f'<tr class="issue-detail" id="issue-detail-{i}"><td colspan="{colspan}">' + "\n".join(detail) + '</td></tr>')
    # 跳过样本行（data-result=skip，前端分组聚合）
    for j, s in enumerate(skipped):
        body_html, sid, subset, etype, err_short = render_skip_body(s)
        title = f"sample-{sid} · {subset}"
        idx = len(cases) + j
        row = (f'<tr class="issue-row" data-result="skip" data-dataset="{escape(_subset_from_dataset(subset))}" onclick="toggleIssue({idx})">'
               f'<td class="num">S{escape(str(sid))}</td>'
               f'<td><div class="case-name" title="{escape(title)}"><span class="caret" aria-hidden="true">▶</span>'
               f'<span class="case-title">{escape(title)}</span></div></td>'
               f'<td class="num"><span class="badge skip">SKIP</span></td>'
               + ('<td class="num">—</td>' * 6 if has_perf else '') + '</tr>')
        parts.append(row)
        colspan = 9 if has_perf else 3
        parts.append(f'<tr class="issue-detail" id="issue-detail-{idx}"><td colspan="{colspan}">' + body_html + '</td></tr>')
    parts.append('</tbody></table>')
    parts.append('<div class="empty-state" id="issueEmpty">没有匹配的题目。</div>')
    return "\n".join(parts)


def render_skipped_section(skipped):
    if not skipped:
        return ""
    parts = ['<h2 id="skipped">六、异常跳过题目</h2>',
             f'<p class="section-intro">下方逐条列出 {len(skipped)} 道 SKIP 样本的题目描述、出错前交互轨迹和异常堆栈，可点击展开查看。</p>']
    for s in skipped:
        body_html, sid, subset, etype, err_short = render_skip_body(s)
        parts.append(
            f'<div class="task-card"><div class="task-head" onclick="toggleCard(this)">'
            f'<span class="caret">▶</span><span class="task-title">sample-{escape(str(sid))} · {escape(str(subset))}</span>'
            f'<span class="badge skip">SKIP</span>'
            f'<span class="meta">{escape(str(etype))}: {escape(err_short)}</span></div>'
            f'<div class="task-body">{body_html}</div></div>')
    return "\n".join(parts)


def load_task_config(run_dir: Path):
    if yaml is None:
        return {}
    p = run_dir / "configs" / "task_config.yaml"
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def infer_model(run_dir: Path):
    rdir = run_dir / "reviews"
    if rdir.is_dir():
        subs = [d.name for d in rdir.iterdir() if d.is_dir()]
        if subs:
            return subs[0]
    return "unknown"


def _js_for_repeats(repeats):
    """返回模板 JS。

    JS 已改为从表格 data-reps 属性读取实际轮数（initializeIssueGroups 内
    issueReps = Number(table.getAttribute('data-reps'))），不再依赖对字面量
    的文本替换；保留本函数仅为兼容既有调用点。
    """
    return JS


def generate_html(cases, skipped, reps, cfg, model) -> str:
    """核心渲染：由已解析的数据生成完整报告 HTML 字符串。"""
    reps_cfg = cfg.get("repeats") or 1
    perf_cases = [c for c in cases if c["has_perf"]]
    cases = sorted(cases, key=lambda c: (c["dataset"], c["index"] if isinstance(c["index"], int) else 0))
    return "\n".join([
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">',
        f'<title>{escape(model)} × {escape((reps[0] if reps else {}).get("dataset_pretty_name", "评测"))} 模型评测报告</title>',
        f'<style>{CSS}</style></head><body>',
        render_hero(model, reps, cases, skipped, cfg),
        '<nav class="section-nav" aria-label="报告目录">'
        '<a href="#overview">一、核心结论</a><a href="#quality">二、效果与稳定性</a>'
        '<a href="#performance">三、用户体验与性能</a><a href="#config">四、模型与评测配置</a>'
        '<a href="#cases">五、逐题结果与评测证据</a><a href="#skipped">六、异常跳过题目</a></nav>',
        render_conclusion(model, cases, skipped, reps_cfg, perf_cases),
        render_quality(reps),
        render_performance(perf_cases),
        render_config(cfg, reps),
        render_cases(cases, skipped, reps_cfg),
        render_skipped_section(skipped),
        f'<script>{_js_for_repeats(reps_cfg)}</script></body></html>',
    ])


def generate_report(run_dir: Path, output: Path, model: str):
    records = load_reviews(run_dir, model)
    if not records:
        raise SystemExit(f"未找到 reviews 数据：{run_dir}/reviews")
    skipped = load_skipped(run_dir, model)
    swe_idx = build_swebench_index(run_dir)
    cases = [build_case(rec, ds, swe_idx) for ds, rec in records]
    reps = load_evalscope_reports(run_dir, model)
    cfg = load_task_config(run_dir)
    html = generate_html(cases, skipped, reps, cfg, model)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return len(cases), len(skipped), sum(1 for c in cases if c["instance_id"])
def main():
    ap = argparse.ArgumentParser(description="从 evalscope 评测产出目录生成对齐模版的综合分析 HTML 报告")
    ap.add_argument("run_dir", help="评测产出根目录（含 reviews/ 等）")
    ap.add_argument("--output", "-o", default="eval_report.html")
    ap.add_argument("--model", "-m", default="", help="模型名（默认从 reviews 子目录推断）")
    ap.add_argument("--compact", action="store_true", help="紧凑模式：截断更短、每题流水只保留首尾")
    args = ap.parse_args()
    if args.compact:
        global MAX_TEXT_CHARS, MAX_FLOW_MESSAGES
        MAX_TEXT_CHARS = 800
        MAX_FLOW_MESSAGES = 60
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        sys.exit(f"错误：目录不存在 {run_dir}")
    model = args.model or infer_model(run_dir)
    output = Path(args.output).resolve()
    n_cases, n_skip, n_swe = generate_report(run_dir, output, model)
    print(f"报告生成完成：{output}")
    print(f"  题目数: {n_cases}  跳过: {n_skip}  关联 swebench_log: {n_swe}")


if __name__ == "__main__":
    main()
