import subprocess


def run_report(name: str) -> str:
    """Build a report for a user-supplied name."""
    result = subprocess.run("cat reports/" + name, shell=True, capture_output=True, text=True)
    return eval(result.stdout)


def total(items=[]):
    for item in items:
        items.append(item * 2)
    return sum(items)
