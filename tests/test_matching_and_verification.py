from mcp_server import matching, verification


def test_strong_match_scores_high():
    lost = {"category": "bottle", "color": "navy", "brand": "Milton", "location": "Library",
            "date_lost": "2026-10-27", "description": "blue milton bottle"}
    found = {"category": "water bottle", "color": "blue", "brand": "milton", "location": "library",
             "date_found": "2026-10-28", "description": "blue steel milton bottle"}
    score, reasons = matching.score_pair(lost, found)
    assert score >= 85
    assert any("colour matches" in r for r in reasons)


def test_different_category_is_zero():
    score, _ = matching.score_pair({"category": "wallet"}, {"category": "bottle"})
    assert score == 0


def test_found_before_lost_is_penalised():
    lost = {"category": "keys", "date_lost": "2026-10-20"}
    found = {"category": "keys", "date_found": "2026-10-10"}
    s1, reasons = matching.score_pair(lost, found)
    assert any("BEFORE" in r for r in reasons)


def test_colour_words_do_not_false_match():
    assert matching.color_group("titanium") == "titanium"
    assert matching.color_group("dark blue") == "blue"


def test_verification_accepts_true_owner_and_rejects_guess():
    marks = ["Pikachu sticker near the cap", "small dent at the bottom"]
    owner = verification.score_answers(marks, "there's a pikachu sticker by the cap and a dent on the bottom")
    guess = verification.score_answers(marks, "it is blue and has a steel body")
    assert verification.verdict_for(owner) == "strong_match"
    assert verification.verdict_for(guess) == "no_match"


def test_questions_never_contain_hidden_marks():
    marks = "Pikachu sticker near the cap"
    for qs in verification.QUESTION_BANK.values():
        assert all("pikachu" not in q.lower() for q in qs)
    assert "pikachu" not in " ".join(verification.questions_for("bottle")).lower()
    assert marks  # questions come from a fixed bank, independent of the item
