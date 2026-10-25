import json
import sys


def walk(node, depth=0):
    name = node.get("name", "")
    print(
        "  " * depth
        + node.get("class", "")
        + " id="
        + node.get("id", "")
        + " type="
        + str(node.get("type"))
        + " name="
        + repr(name[:100])
        + " rect="
        + str(node.get("rect"))
    )
    for child in node.get("children", []):
        walk(child, depth + 1)


walk(json.load(open(sys.argv[1], encoding="utf-8")))
