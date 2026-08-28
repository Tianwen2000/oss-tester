#!/usr/bin/env python3
"""Build the OSS usage, resource-package, and payment test-plan XMind file."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


OUTPUT = Path(__file__).with_name("oss_usage_billing_test_plan.xmind")
TITLE = "OSS 使用统计、资源包与支付测试计划"


TREE = {
    "测试目标与边界": {
        "目标": "验证统计、抵扣、状态和支付链路的正确性、幂等性与可追溯性",
        "范围": "使用统计、资源包管理、报价/订单/支付/账本/开通联动",
        "边界": "统计页面不等同实时账单；延迟或展示精度差异按 WARN 处理",
        "安全": "专用账号、专用 Bucket、隔离前缀；禁止生产批量购买和大额流量",
    },
    "测试准备": {
        "环境": "专用测试 Bucket、测试账号、最小权限身份",
        "资源包": "低金额、小规格；设置预算上限、审批人和告警",
        "基线": "桶数、对象数、容量、读写请求、公网下行、CDN 回源",
        "资源包基线": "订单号、计费项、规格、地域、剩余量、生效和到期时间",
        "唯一范围": "所有对象使用 metering-test/<run-id>/",
        "计量口径": "确认时区、周期、延迟，以及失败/重试请求是否计量",
    },
    "使用统计": {
        "指标": {
            "存储": "桶数量、对象总数、存储容量",
            "流量": "公网下行、CDN 回源",
            "请求": "读请求、写请求及失败/重试口径",
            "筛选": "本月、近 7 日、近 30 日、自定义日期",
        },
        "确定性负载": {
            "对象": "上传 20 个 Key，覆盖写 5 个，删除 2 个",
            "容量": "上传总计 60 MiB 确定性内容",
            "下载": "完整下载 1 MiB 对象 20 次，Range 下载 2 MiB",
            "读请求": "Head 100 次、Get 30 次、List 20 次",
            "写请求": "Put 50 次、Copy 10 次、Delete 10 次",
            "Multipart": "创建并上传 3 个分片后暂停，观察临时存储和请求",
            "CDN 回源": "制造约定次数 MISS，并结合 CDN/源站日志核对",
        },
        "核心断言": {
            "增量": "对象数、容量、流量、请求数与理论负载一致",
            "覆盖与删除": "覆盖写不增加对象数；删除后对象数和容量最终回落",
            "版本桶": "分别核对版本和删除标记",
            "租户隔离": "不串入其他租户或业务桶",
            "页面": "筛选、单位、Tooltip、图例、刷新和分页正确",
            "延迟": "每 15～30 分钟观察，最长覆盖一个完整统计周期",
        },
    },
    "资源包管理": {
        "列表与状态": {
            "字段": "名称、订单号、地域、来源、计费项、规格、用量、起止时间",
            "页签": "未生效、使用中、已耗尽、已过期、退款/取消分类正确",
            "交互": "筛选、排序、分页和总数正确",
            "地域": "测试 Bucket 命中适用地域；不匹配时不得错误抵扣",
        },
        "抵扣与生命周期": {
            "存储包": "容量增长后按产品规则增加用量",
            "流量包": "公网下载后扣减；CDN 回源按计费规则核对",
            "请求包": "固定读写批次后按规则扣减次数",
            "优先级": "多包并存时验证先到期优先或平台声明顺序",
            "耗尽": "首包耗尽后切换下一包，不产生负余额",
            "到期": "到期后停止抵扣",
            "追溯": "购买前用量不能被新包追溯抵扣，除非产品明确支持",
        },
    },
    "支付金额与订单（P0）": {
        "金额模型": {
            "整数金额": "使用最小货币单位，$4.50 = 450 cents，禁止浮点",
            "截图场景": "原价 $8.00 - 优惠 $3.50 = 应付 $4.50 USD",
            "全链路一致": "商品页、报价、确认、订单、支付、账本、资源包金额和币种一致",
            "服务端定价": "后端重新计算并绑定订单，不信任前端展示价格",
        },
        "必测场景": {
            "篡改": "把 $4.50 改为 $0.01，服务端拒绝或忽略",
            "幂等": "重复点击、重复订单、重复回调只扣一次并生成一个资源包",
            "超时": "页面断网后查询原订单，不盲目重新支付",
            "失败": "余额不足、支付失败、取消订单均不扣款、不生效",
            "补偿": "支付成功但开通失败必须退款、补偿或进入人工队列",
            "反向失败": "开通成功但扣款失败时资源包不得可用",
            "价格变化": "优惠过期时重新报价并要求确认",
            "规格一致": "地域、容量、有效期、计费项与下单内容一致",
        },
        "三方对账": {
            "订单": "原价、优惠、税费、应付和币种",
            "支付渠道": "支付金额、币种、状态、网关流水号",
            "账本与资源包": "余额变更、资源包实例、开通状态、抵扣范围",
            "门禁": "一分钱差异、重复扣款或错开通直接 FAIL/P0",
        },
    },
    "执行计划": {
        "1 基线": "记录控制台、账本和资源包原始状态，确认预算审批",
        "2 负载": "执行固定对象、流量和请求负载，保留成功数与时间戳",
        "3 观察": "每 15～30 分钟记录统计、资源包用量和服务端计量",
        "4 支付沙箱": "用最小金额或测试支付验证报价、订单、回调、开通、退款/取消",
        "5 稳定": "跨至少一个计量周期核对最终值、抵扣、到期和耗尽切换",
        "6 清理": "仅删除 metering-test/<run-id>/，不删桶或其他对象",
    },
    "证据与判定": {
        "证据": "截图、统计响应、理论负载、订单号、支付流水、账本、资源包实例 ID、回调日志",
        "PASS": "理论值、平台值和账务值一致，或符合已确认的延迟/精度规则",
        "FAIL": "金额差异、重复扣款、错开通、串租户、核心统计错误或状态不一致",
        "WARN": "统计延迟、展示精度、厂商未公开的计量口径",
        "SKIP": "沙箱未提供某支付方式、地域或资源包类型不适用",
    },
    "发布门禁": {
        "必须通过": "金额链路、幂等、回调重放、失败补偿和三方对账",
        "立即停止": "任何 P0 金额问题都冻结相关功能并停止真实购买",
        "生产验证": "仅在审批、预算上限和告警齐备后人工执行一次小额验证",
        "自动化边界": "自动化做只读核对和支付沙箱验证，不做生产批量购买",
    },
}


CONTENT_NS = "urn:xmind:xmap:xmlns:content:2.0"
STYLE_NS = "urn:xmind:xmap:xmlns:style:2.0"
META_NS = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
MANIFEST_NS = "urn:xmind:xmap:xmlns:manifest:1.0"
NS = {
    "": CONTENT_NS,
    "fo": "http://www.w3.org/1999/XSL/Format",
    "svg": "http://www.w3.org/2000/svg",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "xlink": "http://www.w3.org/1999/xlink",
}


def qname(tag: str) -> str:
    return f"{{{CONTENT_NS}}}{tag}"


def topic_id(path: tuple[str, ...]) -> str:
    digest = hashlib.sha1("/".join(path).encode("utf-8")).hexdigest()[:12]
    return f"topic-{digest}"


def add_topic(parent: ET.Element, title: str, value: object, path: tuple[str, ...]) -> None:
    topic = ET.SubElement(parent, qname("topic"), {"id": topic_id(path)})
    ET.SubElement(topic, qname("title")).text = title
    if isinstance(value, dict):
        children = ET.SubElement(topic, qname("children"))
        attached = ET.SubElement(children, qname("topics"), {"type": "attached"})
        for child_title, child_value in value.items():
            add_topic(attached, child_title, child_value, path + (child_title,))
    elif value:
        note = ET.SubElement(topic, qname("notes"))
        plain = ET.SubElement(note, qname("plain"))
        plain.text = str(value)


def build_content() -> bytes:
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    root = ET.Element(qname("xmap-content"), {"version": "2.0"})
    sheet = ET.SubElement(root, qname("sheet"), {"id": "sheet-oss-usage-billing"})
    ET.SubElement(sheet, qname("title")).text = TITLE
    topic = ET.SubElement(sheet, qname("topic"), {
        "id": "root-topic",
        "structure-class": "org.xmind.ui.map.unbalanced",
    })
    ET.SubElement(topic, qname("title")).text = TITLE
    children = ET.SubElement(topic, qname("children"))
    attached = ET.SubElement(children, qname("topics"), {"type": "attached"})
    for title, value in TREE.items():
        add_topic(attached, title, value, (title,))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_styles() -> bytes:
    root = ET.Element(f"{{{STYLE_NS}}}xmap-styles", {"version": "2.0"})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_meta() -> bytes:
    root = ET.Element(f"{{{META_NS}}}meta")
    ET.SubElement(root, f"{{{META_NS}}}generator").text = "oss-tester XMind plan builder"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_manifest() -> bytes:
    root = ET.Element(f"{{{MANIFEST_NS}}}manifest")
    entries = {
        "content.xml": "text/xml",
        "styles.xml": "text/xml",
        "meta.xml": "text/xml",
    }
    for path, media_type in entries.items():
        ET.SubElement(root, f"{{{MANIFEST_NS}}}file-entry", {
            "full-path": path,
            "media-type": media_type,
        })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content.xml", build_content())
        archive.writestr("styles.xml", build_styles())
        archive.writestr("meta.xml", build_meta())
        archive.writestr("META-INF/manifest.xml", build_manifest())
        archive.writestr("metadata.json", json.dumps({"title": TITLE, "creator": "oss-tester"}, ensure_ascii=False))
    print(OUTPUT)


if __name__ == "__main__":
    main()
