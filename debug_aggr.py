import re

expression = "SUM(aggr(SUM(Sales), Customer))"
outer_aggr_regex = re.compile(
    r"\b(?P<outer>SUM|COUNT|AVERAGE|AVG|MIN|MAX)\s*\(\s*Aggr\s*\((?P<rest>.*)",
    re.IGNORECASE | re.DOTALL,
)

match = outer_aggr_regex.search(expression)
print("Match:", match)
if match:
    print("Groups:", match.group("outer"))

    start = match.start()
    outer_name = match.group("outer").upper()

    aggr_start = expression.find("Aggr", start)
    if aggr_start == -1:
        aggr_start = expression.find("aggr", start)
        
    print("aggr_start:", aggr_start)
    paren_start = expression.find("(", aggr_start)
    depth = 1
    curr = paren_start + 1
    while curr < len(expression) and depth > 0:
        if expression[curr] == "(":
            depth += 1
        elif expression[curr] == ")":
            depth -= 1
        curr += 1
        
    print("curr after aggr:", curr)
    aggr_content = expression[paren_start + 1 : curr - 1]
    print("aggr_content:", aggr_content)
    aggr_end = curr

    outer_end = aggr_end
    while outer_end < len(expression) and expression[outer_end].isspace():
        outer_end += 1
    if outer_end < len(expression) and expression[outer_end] == ")":
        outer_end += 1
        print("outer_end closed properly")
    else:
        print("outer_end NOT closed properly! char at outer_end:", expression[outer_end:outer_end+1])
