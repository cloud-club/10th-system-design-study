import json
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

import build
from presentation import parse_summary, request_summary


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.topics = json.loads(Path(build.ROOT / "topics.json").read_text())

    def test_existing_presentation_path_and_title(self):
        path = "week02/seoyoung/대규모 지표 모니터링 및 경보 시스템 설계 - 윤서영.pdf"
        pr = {"title": "[week02] 윤서영 - 지표 모니터링 설계 발표 내용"}
        self.assertTrue(build.is_presentation(path, pr))
        self.assertEqual(build.resolve_topic(self.topics, pr, [path]), "week02-monitoring")

    def test_only_merged_html_presentations_open_on_pages(self):
        payment = "week04/presentation/결제 시스템 설계 - 고태현.html"
        wallet = "week04/presentation/전자 지갑 설계 - 발표자.html"
        repo = "club/study"
        snapshot = {
            "repo": repo, "branch": "main", "base_files": [payment],
            "pulls": [{
                "number": 9, "state": "open", "title": "[Week 04] 전자 지갑 발표",
                "head": {"repo": {"full_name": repo}, "sha": "preview"},
                "files": [{"filename": wallet}],
            }],
        }
        raw_html = b"<!doctype html><title>Presentation</title>"

        def read(source_repo, ref, file):
            self.assertEqual((source_repo, ref, file["filename"]), (repo, "main", payment))
            return raw_html

        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            data = build.build_model(self.topics, snapshot, read, date(2026, 11, 24),
                                     presentation_dir=output_dir)
            topics = {topic["id"]: topic for topic in data["topics"]}
            published = topics["week04-payment"]["presentations"][0]["url"]
            self.assertEqual(published, "./" + build.quote(payment))
            self.assertEqual((output_dir / payment).read_bytes(), raw_html)
            preview = topics["week04-wallet"]["presentations"][0]["url"]
            self.assertEqual(preview, f"https://github.com/{repo}/blob/preview/{build.quote(wallet)}")
            self.assertFalse((output_dir / wallet).exists())

    def test_week_alone_does_not_mix_topics(self):
        pr = {"title": "[Week 03] 설계 제출"}
        self.assertIsNone(build.resolve_topic(self.topics, pr, ["week03/alex/design.md"]))
        pr["labels"] = [{"name": "topic:week03-message-queue"}]
        self.assertEqual(build.resolve_topic(self.topics, pr, ["week03/alex/design.md"]), "week03-message-queue")

    def test_pdf_filename_disambiguates_a_pr_with_two_talks(self):
        pr = {"title": "[Week 03] 분산 메시지 큐와 예약 시스템 발표 자료"}
        path = "week03/presentation/분산 메시지 큐 - 발표자.pdf"
        self.assertEqual(build.resolve_topic(self.topics, pr, [path]), "week03-message-queue")

    def test_template_examples_do_not_become_designs(self):
        body = "## 3. 아키텍처 설계도\n> 예시: Kafka\n**설계도**\n-\n## 4. 기술 선택 근거\n**내용**\nDB 트랜잭션을 사용합니다.\n"
        self.assertEqual(build.design_fields(body), {"decision": "DB 트랜잭션을 사용합니다."})

    def test_design_markdown_keeps_structure_and_escapes_untrusted_html(self):
        body = ("## 아키텍처 설계도\n\n1. **수집**\n   - 저장\n\n"
                "| 구성 | 역할 |\n| --- | --- |\n| DB | 저장 |\n\n"
                "```bash\n# 기술 선택\n  echo test\n```\n\n"
                "<script>alert(1)</script>\n\n[링크](javascript:alert%281%29)\n"
                "## 기술 선택 근거\n트랜잭션을 사용합니다.")
        fields = build.design_fields(body)
        html = build.MARKDOWN.render(fields["architecture"])
        for fragment in ("<ol>", "<ul>", "<strong>수집</strong>", "<table>", "  echo test", "&lt;script&gt;"):
            self.assertIn(fragment, html)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript:', html)
        self.assertEqual(fields["decision"], "트랜잭션을 사용합니다.")

    def test_real_week03_headings_keep_nested_diagrams_and_decisions(self):
        body = ("## 3. 아키텍처 설계도\n### 전체 구조\n![전체](./images/base.svg)\n예약과 재고를 함께 관리합니다.\n"
                "## 4. 기술 선택 근거\n### 4.1. DB 잠금\n동일 트랜잭션으로 갱신합니다.\n"
                "## 5. 트레이드오프\n잠금 대기가 발생합니다.")
        self.assertIn("예약과 재고", build.design_fields(body)["architecture"])
        self.assertIn("동일 트랜잭션", build.design_fields(body)["decision"])
        images = list(build.design_images(body, "club/study", "abc", "week03/member"))
        self.assertEqual(images[0]["url"], "https://raw.githubusercontent.com/club/study/abc/week03/member/images/base.svg")
        self.assertEqual(build.resolve_topic(self.topics, {"title": "[Week 03] 신재호 - 트래픽 완충 시스템 설계"},
                                            ["week03/jaeho/README.md"]), "week03-message-queue")
        self.assertTrue(build.is_presentation("week03/presentation/호텔 예약 시스템 설계 - 김동욱.html", {}))

    def test_ai_requires_valid_pdf_page_references(self):
        item = {"text": "원문 근거", "pages": [2]}
        result = {key: [item] for key in ("background", "requirements", "flow")}
        response = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(result)}
        ]}]}
        self.assertEqual(parse_summary(response, 2), result)
        with self.assertRaises(ValueError):
            parse_summary(response, 1)

    def test_pdf_request_uses_structured_response_and_local_pdf_bytes(self):
        response = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"background":[],"requirements":[],"flow":[]}'}
        ]}]}
        with patch("presentation.urlopen", return_value=BytesIO(json.dumps(response).encode())) as send:
            result = request_summary(b"%PDF-example", "gpt-5-mini", "test-key", 2)
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertTrue(payload["input"][0]["content"][0]["file_data"].startswith("data:application/pdf;base64,"))
        self.assertNotIn("test-key", request.data.decode())
        self.assertEqual(result["requirements"], [])

    def test_open_and_merged_designs_are_grouped_by_topic(self):
        def pr(number, topic, **extra):
            return {
                "number": number, "title": f"[Week 03] 설계 {number}", "state": "open",
                "labels": [{"name": "topic:" + topic}], "files": [],
                "body": "## 4. 기술 선택 근거\n비동기 처리를 사용합니다.\n## 공통 고려사항\n- [x] 장애 복구",
                **extra,
            }
        snapshot = {"repo": "cloud-club/10th-system-design-study", "branch": "main", "pulls": [
            pr(2, "week03-message-queue"),
            pr(3, "week03-reservation-notification"),
            pr(4, "week03-message-queue", state="closed", merged_at="2026-11-12"),
            pr(5, "week03-message-queue", state="closed"),
        ]}
        model = build.build_model(self.topics, snapshot, lambda *_: None, date(2026, 11, 9))
        self.assertEqual(model["meetingDate"], "2026-11-11")
        topics = {topic["id"]: topic for topic in model["topics"]}
        queue = topics["week03-message-queue"]
        self.assertEqual([(p["number"], p["state"]) for p in queue["designs"]], [(2, "open"), (4, "merged")])
        self.assertEqual(queue["common"], {"open": [], "all": ["장애 복구"]})
        self.assertEqual([p["number"] for p in topics["week03-reservation-notification"]["designs"]], [3])

    def test_design_diagrams_are_read_from_open_pull_body_and_branch(self):
        attachment = "https://github.com/user-attachments/assets/12345678-abcd"
        head = {"repo": {"full_name": "member/study"}, "sha": "abc123"}
        base = {"title": "[week02] 모니터링 설계", "state": "open", "head": head}
        doc = "week02/member/README.md"
        snapshot = {"repo": "cloud-club/10th-system-design-study", "branch": "main", "pulls": [
            {**base, "number": 2, "files": [{"filename": doc}], "body": (
                "## 아키텍처 설계도\n<img alt=\"전체 구조\" src=\"" + attachment + "\">\n"
                "수집과 집계를 분리합니다.\n## 기술 선택 근거\n이벤트를 버퍼링합니다.\n"
                "## 트레이드오프\n처리 지연이 생깁니다."
            )},
            {**base, "number": 3, "files": [], "body": (
                "![배지](" + attachment + ")\n## 아키텍처 설계도\n"
                "![잘못된 링크](javascript:alert(1))\n설계 설명만 올렸습니다."
            )},
            {**base, "number": 4, "files": [{"filename": "week02/member/architecture.svg"}],
             "body": "## 기술 선택 근거\n집계를 분리합니다."},
        ]}

        def read(repo, ref, file):
            self.assertEqual((repo, ref, file["filename"]), ("member/study", "abc123", doc))
            return "## 아키텍처 설계도\n![집계 흐름](<./집계 흐름.png>)".encode()

        model = build.build_model(self.topics, snapshot, read, date(2026, 11, 9))
        monitoring = next(topic for topic in model["topics"] if topic["id"] == "week02-monitoring")
        first, missing, named = monitoring["designs"]
        self.assertEqual(first["architecture"], "수집과 집계를 분리합니다.")
        self.assertEqual(first["images"], [
            {"url": attachment, "alt": "전체 구조"},
            {"url": "https://raw.githubusercontent.com/member/study/abc123/week02/member/"
                    "%EC%A7%91%EA%B3%84%20%ED%9D%90%EB%A6%84.png", "alt": "집계 흐름"},
        ])
        self.assertEqual(missing["images"], [])
        self.assertEqual(missing["architecture"], "설계 설명만 올렸습니다.")
        self.assertEqual(named["images"][0]["url"],
                         "https://raw.githubusercontent.com/member/study/abc123/week02/member/architecture.svg")

    def test_comparison_uses_images_and_separate_pr_filters(self):
        attachment = "https://github.com/user-attachments/assets/12345678-abcd"
        pulls = [{
            "number": number, "title": f"[week02] 작성자 {number} - 모니터링 설계",
            "state": "closed" if number == 4 else "open",
            "merged_at": "2026-11-10" if number == 4 else None,
            "head": {"repo": {"full_name": "member/study"}, "sha": "abc123"},
            "author_association": "NONE" if number == 5 else "MEMBER",
            "files": [], "body": f"## 아키텍처 설계도\n![설계도]({attachment})\n지표를 수집합니다.",
        } for number in (2, 3, 4, 5)]
        snapshot = {"repo": "cloud-club/10th-system-design-study", "branch": "main", "pulls": pulls}
        def respond(request, **_):
            content = json.loads(request.data)["input"][0]["content"]
            self.assertTrue(any(item.get("image_url", "").startswith("data:image/png;base64,")
                                for item in content))
            self.assertNotIn("PR #5", str(content))
            return BytesIO(json.dumps({"status": "completed", "output": [{"type": "message", "content": [{
                "type": "output_text", "text": json.dumps({
                    "common": [{"text": "지표를 수집합니다.", "evidence": [
                        {"number": 2, "image": 1}, {"number": 3, "image": 0},
                    ]}],
                    "differences": [],
                }),
            }]}]}).encode())

        for pr in pulls:
            pr["user"] = {"login": f"member-{pr['number']}"}
        with (patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
              patch("comparison.read_image", return_value="data:image/png;base64,AAAA") as download,
              patch("comparison.urlopen", side_effect=respond) as send):
            result = build.build_model(self.topics, snapshot, lambda *_: None, date(2026, 11, 10))
        topic = next(topic for topic in result["topics"] if topic["id"] == "week02-monitoring")
        self.assertEqual(topic["comparison"]["open"]["numbers"], [2, 3])
        self.assertEqual(topic["comparison"]["all"]["numbers"], [2, 3, 4])
        self.assertEqual([design["number"] for design in topic["designs"]], [2, 3, 5, 4])
        self.assertEqual(topic["comparison"]["open"]["imageNumbers"], [2, 3])
        download.assert_called_once_with(attachment)
        self.assertEqual(send.call_count, 2)

    def test_comparison_rejects_evidence_that_was_not_sent(self):
        from comparison import validate_comparison
        summary = {"common": [{"text": "같은 방식을 씁니다.", "evidence": [
            {"number": 2, "image": 2}, {"number": 3, "image": 0},
        ]}], "differences": []}
        with self.assertRaisesRegex(ValueError, "invalid comparison image"):
            validate_comparison(summary, {2: ("alice", {1}), 3: ("bob", {0})})

    def test_attachment_download_allows_github_storage_only(self):
        from comparison import GitHubImageRedirect
        redirect = GitHubImageRedirect()
        original = Request("https://github.com/user-attachments/assets/diagram-id")
        target = "https://github-production-user-asset-6210df.s3.amazonaws.com/example.png"
        self.assertEqual(redirect.redirect_request(original, None, 302, "", {}, target).full_url, target)
        with self.assertRaises(URLError):
            redirect.redirect_request(original, None, 302, "", {}, "https://example.com/image.png")


if __name__ == "__main__":
    unittest.main()
