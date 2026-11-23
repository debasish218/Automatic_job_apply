from job_agent.filters import extract_min_years, is_india, title_check


def test_india_locations():
    assert is_india("Hyderabad, Telangana")
    assert is_india("Bengaluru, India")
    assert is_india("Remote - India")
    assert not is_india("San Francisco, CA")
    assert not is_india("Indianapolis, IN")


def test_titles():
    assert title_check("Machine Learning Engineer")[0]
    assert title_check("MLOps Engineer")[0]
    assert title_check("Computer Vision Engineer")[0]
    assert title_check("Applied AI Engineer, Partner - AWS")[0]
    assert title_check("Data Scientist")[0]
    assert not title_check("Senior Machine Learning Engineer")[0]
    assert not title_check("Staff AI Platform Engineer")[0]
    assert not title_check("Data Scientist III")[0]
    assert not title_check("AI Success Manager")[0]
    assert not title_check("Backend Engineer")[0]
    assert not title_check("Product Management - AI Payments")[0]
    assert title_check("Member Technical Staff - Applied AI Engineer")[0]


def test_min_years():
    assert extract_min_years("We need 1-3 years of experience in ML.")[0] == 1.0
    assert extract_min_years("Minimum of 4 years of industry experience")[0] == 4.0
    assert extract_min_years("5+ years experience building systems. Also 2+ years with PyTorch experience.")[0] == 2.0
    assert extract_min_years("Experience: 0 to 2 years")[0] == 0.0
    assert extract_min_years("Great team, 100 people, founded 10 years ago")[0] is None
    assert extract_min_years("")[0] is None


def test_interns_rejected():
    assert title_check("AI / Machine Learning Intern")[0] is False
    assert title_check("Machine Learning Trainee")[0] is False
    assert title_check("Internal Tools ML Engineer")[0] is True     # 'internal' is not 'intern'
