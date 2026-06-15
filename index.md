---
layout: default
title: AI Majiacheng
---

# AI Majiacheng

每日 AI 情报雷达。每天泰国时间 07:00 自动抓取、筛选和分析前一天的 AI 动态。

## 最新日报

{% assign zh_posts = site.posts | where: "lang", "zh" %}
{% assign latest = zh_posts | first %}
{% if latest %}

### [{{ latest.date | date: "%Y-%m-%d" }} AI 日报]({{ latest.url | relative_url }})

{{ latest.excerpt | strip_html | truncate: 180 }}

{% else %}

暂无日报。首次定时任务完成后会自动出现在这里。

{% endif %}

## 归档

<ul>
  {% for post in zh_posts limit:30 %}
    <li>
      <a href="{{ post.url | relative_url }}">{{ post.date | date: "%Y-%m-%d" }}</a>
    </li>
  {% else %}
    <li><em>暂无内容</em></li>
  {% endfor %}
</ul>

## 订阅

- [RSS]({{ '/feed-zh.xml' | relative_url }})
- [GitHub](https://github.com/captain-jeric/ai-majiacheng-com)

