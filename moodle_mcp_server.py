#!/usr/bin/env python3
"""
Moodle MCP Server
A Model Context Protocol server for interacting with Moodle LMS.
"""

import asyncio
import hmac
import os
import logging
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()
logging.basicConfig(level=logging.CRITICAL)
for logger_name in ("httpx", "httpcore", "mcp", "mcp.server", "mcp.server.lowlevel"):
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)

# MCP Inspector currently expects a global FastMCP object named app/server/mcp.
# json_response + stateless_http maximize compatibility with simple/enterprise
# HTTP clients (e.g. M365 connector discovery) that expect a single plain JSON
# response per call rather than an SSE stream tied to a persistent session.
app = FastMCP(
    "moodle-server",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("PORT", "8000")),
    json_response=True,
    stateless_http=True,
)

# Configuration
MOODLE_URL = os.getenv("MOODLE_URL", "https://lt1.insuranceinstitute.ca").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "")


def get_moodle_api_url() -> str:
    """Construct the Moodle Web Services API URL."""
    return f"{MOODLE_URL}/webservice/rest/server.php"


_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared, connection-pooled HTTP client for Moodle requests."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Project a Moodle user record onto a minimal, stable schema for MCP responses."""
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "fullname": user.get("fullname"),
        "email": user.get("email"),
    }


async def call_moodle_api(function: str, params: dict[str, Any] | None = None) -> Any:
    """
    Make an API call to Moodle Web Services.

    Args:
        function: Moodle Web Service function name.
        params: Additional parameters for the function.

    Returns:
        JSON response from Moodle, or a normalized {"error": ...} dict on failure.
    """
    if params is None:
        params = {}

    if not MOODLE_TOKEN:
        return {"error": "MOODLE_TOKEN is not set. Add it to .env or your environment."}

    request_params = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": function,
        "moodlewsrestformat": "json",
    }
    request_params.update(params)

    try:
        response = await get_http_client().post(
            get_moodle_api_url(),
            data=request_params,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as e:
        return {"error": f"HTTP error occurred: {str(e)}"}
    except Exception as e:
        return {"error": f"An error occurred: {str(e)}"}

    if isinstance(data, dict) and ("exception" in data or "errorcode" in data):
        return {
            "error": data.get("message", "Moodle API error"),
            "errorcode": data.get("errorcode"),
            "exception": data.get("exception"),
            "function": function,
        }

    return data


@app.tool()
async def get_site_info() -> Any:
    """Get information about the Moodle site including version, user details, and available functions."""
    return await call_moodle_api("core_webservice_get_site_info")


@app.tool()
async def get_courses() -> str:
    """Disabled safety guard: use search_courses instead to avoid loading the full Moodle course database."""
    return (
        "The get_courses tool is disabled because this Moodle site has "
        "a very large course database. Use search_courses with a "
        "specific query instead."
    )


@app.tool()
async def get_course_contents(courseid: int) -> Any:
    """Get course metadata plus the contents and structure of a specific course."""
    details = await get_course_details(courseid)
    contents = await call_moodle_api("core_course_get_contents", {"courseid": courseid})
    return {
        "course": details,
        "contents": contents,
    }


@app.tool()
async def get_course_name(courseid: int) -> str:
    """Get the display name/fullname for a specific Moodle course id."""
    details = await get_course_details(courseid)
    courses = details.get("courses", []) if isinstance(details, dict) else []
    if not courses:
        return f"No course found for course id {courseid}."

    course = courses[0]
    fullname = course.get("fullname") or course.get("displayname") or course.get("shortname")
    category = course.get("categoryname")
    if category:
        return f"{fullname} (course id {courseid}, category: {category})"
    return f"{fullname} (course id {courseid})"


@app.tool()
async def get_course_details(courseid: int) -> Any:
    """Get detailed Moodle course metadata for a specific course id."""
    return await call_moodle_api(
        "core_course_get_courses_by_field",
        {
            "field": "id",
            "value": courseid,
        },
    )


@app.tool()
async def get_course_participants(courseid: int, limit: int = 50, offset: int = 0) -> Any:
    """Get participants enrolled in a specific course, limited to avoid very large responses."""
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    params = {
        "courseid": courseid,
        "options[0][name]": "onlyactive",
        "options[0][value]": "1",
        "options[1][name]": "limitfrom",
        "options[1][value]": str(safe_offset),
        "options[2][name]": "limitnumber",
        "options[2][value]": str(safe_limit),
    }
    return await call_moodle_api("core_enrol_get_enrolled_users", params)


@app.tool()
async def check_user_in_course(courseid: int, userid: int) -> Any:
    """Check whether a specific Moodle user is enrolled/participating in a specific course."""
    courses = await get_user_courses(userid)
    if not isinstance(courses, list):
        return {
            "courseid": courseid,
            "userid": userid,
            "enrolled": False,
            "error": courses,
        }

    course = next((course for course in courses if course.get("id") == courseid), None)
    return {
        "courseid": courseid,
        "userid": userid,
        "enrolled": course is not None,
        "course": course,
    }


@app.tool()
async def get_user_by_id(userid: int) -> Any:
    """Get Moodle user profile information for a specific user id."""
    users = await call_moodle_api(
        "core_user_get_users_by_field",
        {
            "field": "id",
            "values[0]": userid,
        },
    )
    return [public_user(user) for user in users] if isinstance(users, list) else users


@app.tool()
async def get_user_by_username(username: str) -> Any:
    """Get Moodle user profile information by exact username, such as the CRM member identifier."""
    users = await call_moodle_api(
        "core_user_get_users_by_field",
        {
            "field": "username",
            "values[0]": username,
        },
    )
    return [public_user(user) for user in users] if isinstance(users, list) else users


@app.tool()
async def get_member_by_username(username: str) -> Any:
    """Get a Moodle member by exact CRM/member username."""
    return await get_user_by_username(username)


@app.tool()
async def get_member_by_id(member_id: str) -> Any:
    """Get a Moodle member by CRM/member id."""
    return await get_user_by_username(member_id)


@app.tool()
async def get_user_by_email(email: str) -> Any:
    """Get Moodle user profile information by exact email address."""
    users = await call_moodle_api(
        "core_user_get_users_by_field",
        {
            "field": "email",
            "values[0]": email,
        },
    )
    return [public_user(user) for user in users] if isinstance(users, list) else users


@app.tool()
async def search_users(query: str, field: str = "username", limit: int = 25) -> Any:
    """
    Search Moodle users by username/member id, name, email, idnumber, or id.

    Valid fields depend on Moodle permissions, but username is the CRM-friendly default.
    """
    allowed_fields = {"username", "firstname", "lastname", "email", "idnumber", "id"}
    search_field = field.strip().lower()
    if search_field not in allowed_fields:
        return {
            "error": (
                f"Unsupported user search field '{field}'. Use one of: "
                f"{', '.join(sorted(allowed_fields))}."
            )
        }

    safe_limit = max(1, min(limit, 100))
    result = await call_moodle_api(
        "core_user_get_users",
        {
            "criteria[0][key]": search_field,
            "criteria[0][value]": query,
        },
    )

    if isinstance(result, dict) and isinstance(result.get("users"), list):
        result["users"] = result["users"][:safe_limit]
        result["returned"] = len(result["users"])
        result["limit"] = safe_limit

    return result


@app.tool()
async def find_users_by_name(firstname: str, lastname: str, limit: int = 25) -> Any:
    """Find Moodle users by firstname and lastname."""
    safe_limit = max(1, min(limit, 100))
    result = await call_moodle_api(
        "core_user_get_users",
        {
            "criteria[0][key]": "firstname",
            "criteria[0][value]": firstname,
            "criteria[1][key]": "lastname",
            "criteria[1][value]": lastname,
        },
    )

    if isinstance(result, dict) and isinstance(result.get("users"), list):
        result["users"] = result["users"][:safe_limit]
        result["returned"] = len(result["users"])
        result["limit"] = safe_limit

    return result


@app.tool()
async def lookup_person(identifier: str) -> Any:
    """
    Look up a Moodle person by user id, username/member id, email, or full name.

    This is intended for natural LLM use when the user says something like
    "Aaron Beal" or "member 240789".
    """
    value = identifier.strip()
    if not value:
        return {"error": "identifier is required"}

    if value.isdigit():
        by_username = await get_user_by_username(value)
        if isinstance(by_username, list) and by_username:
            return {"match_type": "username", "users": by_username}

        by_id = await get_user_by_id(int(value))
        return {"match_type": "userid", "users": by_id}

    if "@" in value:
        return {"match_type": "email", "users": await get_user_by_email(value)}

    parts = value.split()
    if len(parts) >= 2:
        firstname = parts[0]
        lastname = " ".join(parts[1:])
        result = await find_users_by_name(firstname, lastname)
        return {"match_type": "name", **result} if isinstance(result, dict) else result

    # Single-token names could be a firstname or a lastname; check both and merge.
    firstname_result = await search_users(value, "firstname")
    lastname_result = await search_users(value, "lastname")
    firstname_users = (
        firstname_result.get("users", []) if isinstance(firstname_result, dict) else []
    )
    lastname_users = (
        lastname_result.get("users", []) if isinstance(lastname_result, dict) else []
    )

    seen_ids: set[Any] = set()
    merged_users = []
    for user in firstname_users + lastname_users:
        uid = user.get("id")
        if uid not in seen_ids:
            seen_ids.add(uid)
            merged_users.append(user)

    return {
        "match_type": "name",
        "users": merged_users,
        "returned": len(merged_users),
    }


@app.tool()
async def get_user_courses(userid: int) -> Any:
    """Get courses a specific Moodle user is enrolled in."""
    return await call_moodle_api("core_enrol_get_users_courses", {"userid": userid})


@app.tool()
async def get_course_instructors(courseid: int) -> Any:
    """
    Get instructor details for a specific course.

    Matches role shortnames from MOODLE_INSTRUCTOR_ROLES (comma-separated),
    defaulting to editingteacher, teacher, and instructor. Returned contact
    fields depend on what the Moodle web service token is allowed to see.
    """
    instructor_roles = {
        role.strip().lower()
        for role in os.getenv(
            "MOODLE_INSTRUCTOR_ROLES", "editingteacher,teacher,instructor"
        ).split(",")
        if role.strip()
    }
    instructors: list[dict[str, Any]] = []
    complete = True
    limit = 200
    offset = 0

    while True:
        page = await call_moodle_api(
            "core_enrol_get_enrolled_users",
            {
                "courseid": courseid,
                "options[0][name]": "limitfrom",
                "options[0][value]": str(offset),
                "options[1][name]": "limitnumber",
                "options[1][value]": str(limit),
            },
        )

        if not isinstance(page, list):
            return {
                "courseid": courseid,
                "instructors": instructors,
                "complete": False,
                "error": page,
            }

        for participant in page:
            roles = participant.get("roles")
            if roles is None:
                complete = False
                continue

            matched_roles = [
                role
                for role in roles
                if str(role.get("shortname", "")).lower() in instructor_roles
            ]
            if not matched_roles:
                continue

            instructors.append(
                {
                    "id": participant.get("id"),
                    "fullname": participant.get("fullname"),
                    "firstname": participant.get("firstname"),
                    "lastname": participant.get("lastname"),
                    "email": participant.get("email"),
                    "description": participant.get("description"),
                    "descriptionformat": participant.get("descriptionformat"),
                    "department": participant.get("department"),
                    "institution": participant.get("institution"),
                    "profileimageurl": participant.get("profileimageurl"),
                    "roles": matched_roles,
                }
            )

        if len(page) < limit:
            break
        offset += limit

    result: dict[str, Any] = {
        "courseid": courseid,
        "instructors": instructors,
        "complete": complete,
    }
    if not complete:
        result["warning"] = (
            "Moodle omitted roles for some participants; instructors may be missing."
        )
    return result


async def _include_course_instructors(courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach instructor details to Moodle course dictionaries, with bounded concurrency."""
    semaphore = asyncio.Semaphore(5)

    async def enrich(course: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            enriched_course = dict(course)
            enriched_course["instructor_details"] = await get_course_instructors(course["id"])
            return enriched_course

    return list(await asyncio.gather(*(enrich(course) for course in courses)))


@app.tool()
async def get_user_instructors(userid: int) -> Any:
    """
    Get instructor details grouped by every course a Moodle user is enrolled in.

    Use get_user_by_username first when starting from a CRM/member id.
    """
    courses = await get_user_courses(userid)
    if not isinstance(courses, list):
        return {"userid": userid, "error": courses}

    return {
        "userid": userid,
        "courses": await _include_course_instructors(courses),
    }


@app.tool()
async def get_member_profile_and_courses(username: str, include_instructors: bool = True) -> Any:
    """Get a Moodle member profile by CRM/member username and include enrolled courses and instructors."""
    users = await get_user_by_username(username)
    if not isinstance(users, list):
        return {"username": username, "error": users}
    if not users:
        return {
            "username": username,
            "user": None,
            "courses": [],
            "message": "No Moodle user found for that username.",
        }

    user = users[0]
    courses = await get_user_courses(user["id"])
    if include_instructors and isinstance(courses, list):
        courses = await _include_course_instructors(courses)
    return {
        "user": user,
        "courses": courses,
    }


@app.tool()
async def get_user_registration_history(userid: int) -> Any:
    """
    Get a Moodle user's registration history: every course they are enrolled in,
    with course completion status/date where course completion tracking is enabled.

    Moodle does not expose an enrollment timestamp via standard web services, so
    entries are ordered by course start date as a chronological approximation.
    """
    courses = await get_user_courses(userid)
    if not isinstance(courses, list):
        return {"userid": userid, "error": courses}

    semaphore = asyncio.Semaphore(5)

    async def with_completion(course: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            completion = await call_moodle_api(
                "core_completion_get_course_completion_status",
                {"courseid": course.get("id"), "userid": userid},
            )
            completion_status = (
                completion.get("completionstatus") if isinstance(completion, dict) else None
            )
            criteria = (
                completion_status.get("completions", [])
                if isinstance(completion_status, dict)
                else []
            )
            completion_times = [
                entry.get("timecompleted")
                for entry in criteria
                if isinstance(entry, dict) and entry.get("timecompleted")
            ]
            return {
                "courseid": course.get("id"),
                "fullname": course.get("fullname") or course.get("displayname") or course.get("shortname"),
                "shortname": course.get("shortname"),
                "startdate": course.get("startdate"),
                "enddate": course.get("enddate"),
                "completed": (
                    bool(completion_status.get("completed"))
                    if isinstance(completion_status, dict)
                    else None
                ),
                "timecompleted": max(completion_times) if completion_times else None,
                "completion_unavailable": (
                    completion.get("error") if isinstance(completion, dict) and "error" in completion else None
                ),
            }

    history = list(await asyncio.gather(*(with_completion(course) for course in courses)))
    history.sort(key=lambda entry: (entry.get("startdate") is None, entry.get("startdate") or 0))

    return {
        "userid": userid,
        "registration_count": len(history),
        "history": history,
    }


@app.tool()
async def get_member_registration_history(username: str) -> Any:
    """Get a Moodle member's registration history by CRM/member username."""
    users = await get_user_by_username(username)
    if not isinstance(users, list):
        return {"username": username, "error": users}
    if not users:
        return {
            "username": username,
            "message": "No Moodle user found for that username.",
        }

    return await get_user_registration_history(users[0]["id"])


@app.tool()
async def lookup_member_course_status(member_or_user: str, courseid: int) -> Any:
    """Check whether a member/user is enrolled in a course, accepting member id, user id, email, or full name."""
    person = await lookup_person(member_or_user)
    users = person.get("users", []) if isinstance(person, dict) else []
    if not users:
        return {
            "member_or_user": member_or_user,
            "courseid": courseid,
            "found_user": False,
            "enrolled": False,
        }

    user = users[0]
    status = await check_user_in_course(courseid, user["id"])
    course_name = await get_course_name(courseid)
    return {
        "member_or_user": member_or_user,
        "course": course_name,
        "user": {
            "id": user.get("id"),
            "username": user.get("username"),
            "fullname": user.get("fullname"),
            "email": user.get("email"),
        },
        "status": status,
    }


@app.tool()
async def check_member_in_course(member_id: str, courseid: int) -> Any:
    """Check whether a CRM/member id is enrolled in a Moodle course."""
    return await lookup_member_course_status(member_id, courseid)


@app.tool()
async def check_student_registration(student_name: str, class_name_or_id: str) -> Any:
    """Check whether a student name is registered/enrolled in a CRM class/Moodle course."""
    person = await lookup_person(student_name)
    users = person.get("users", []) if isinstance(person, dict) else []
    if not users:
        return {
            "student_name": student_name,
            "class_name_or_id": class_name_or_id,
            "found_student": False,
            "registered": False,
            "message": "No Moodle user found for that student name.",
        }

    class_value = class_name_or_id.strip()
    if class_value.isdigit():
        courseid = int(class_value)
        course = await get_course_name(courseid)
    else:
        class_details = await get_class_details(class_value)
        exact_match = class_details.get("exact_match") if isinstance(class_details, dict) else None
        if not exact_match:
            matches = class_details.get("matches", []) if isinstance(class_details, dict) else []
            return {
                "student_name": student_name,
                "class_name_or_id": class_name_or_id,
                "found_student": True,
                "registered": False,
                "students": [
                    {
                        "id": user.get("id"),
                        "username": user.get("username"),
                        "fullname": user.get("fullname"),
                        "email": user.get("email"),
                    }
                    for user in users
                ],
                "ambiguous": len(matches) > 1,
                "matches": matches,
                "message": (
                    "Multiple Moodle courses matched that class name; specify a course id "
                    "to disambiguate."
                    if len(matches) > 1
                    else "Student found, but no Moodle course found for that class."
                ),
            }
        courseid = exact_match["id"]
        course = exact_match.get("fullname") or exact_match.get("displayname") or exact_match.get("shortname")

    checks = []
    for user in users:
        status = await check_user_in_course(courseid, user["id"])
        checks.append(
            {
                "user": {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "fullname": user.get("fullname"),
                    "email": user.get("email"),
                },
                "registered": bool(status.get("enrolled")),
            }
        )

    return {
        "student_name": student_name,
        "class_name_or_id": class_name_or_id,
        "courseid": courseid,
        "course": course,
        "registered": any(check["registered"] for check in checks),
        "matches": checks,
    }


@app.tool()
async def get_assignments(courseids: list[int]) -> Any:
    """Get assignments from one or more courses."""
    params = {f"courseids[{i}]": cid for i, cid in enumerate(courseids)}
    return await call_moodle_api("mod_assign_get_assignments", params)


@app.tool()
async def get_user_grades(courseid: int, userid: int = 0) -> Any:
    """Get grade information for a user in a course. Use userid=0 for the current user."""
    return await call_moodle_api(
        "gradereport_user_get_grade_items",
        {"courseid": courseid, "userid": userid},
    )


@app.tool()
async def search_courses(search: str) -> Any:
    """Search for courses by text, or pass a numeric course id to get exact course details."""
    if search.strip().isdigit():
        details = await get_course_details(int(search.strip()))
        courses = details.get("courses", []) if isinstance(details, dict) else []
        if not courses:
            return details

        course = courses[0]
        return {
            "id": course.get("id"),
            "fullname": course.get("fullname"),
            "displayname": course.get("displayname"),
            "shortname": course.get("shortname"),
            "categoryid": course.get("categoryid"),
            "categoryname": course.get("categoryname"),
            "visible": course.get("visible"),
            "startdate": course.get("startdate"),
            "enddate": course.get("enddate"),
            "format": course.get("format"),
            "enrollmentmethods": course.get("enrollmentmethods", []),
            "warnings": details.get("warnings", []),
        }

    return await call_moodle_api(
        "core_course_search_courses",
        {"criterianame": "search", "criteriavalue": search},
    )


@app.tool()
async def search_classes(class_name: str) -> Any:
    """Search CRM class names by mapping them to Moodle course search."""
    return await search_courses(class_name)


@app.tool()
async def get_class_details(class_name_or_id: str) -> Any:
    """
    Get Moodle course details using a CRM class name or Moodle course id.

    Text searches only populate exact_match when exactly one course matches, or
    exactly one result has an id/shortname/fullname that equals the input
    verbatim. Otherwise the caller must disambiguate using the returned matches.
    """
    value = class_name_or_id.strip()
    if value.isdigit():
        return await get_course_details(int(value))

    # CRM class codes usually map to the Moodle course shortname/idnumber
    # verbatim, so try an exact field match before falling back to fuzzy search.
    shortname_result = await call_moodle_api(
        "core_course_get_courses_by_field",
        {"field": "shortname", "value": value},
    )
    shortname_courses = (
        shortname_result.get("courses", []) if isinstance(shortname_result, dict) else []
    )
    if len(shortname_courses) == 1:
        course = shortname_courses[0]
        return {
            "class_name_or_id": class_name_or_id,
            "matches": [course],
            "match_count": 1,
            "exact_match": course,
            "ambiguous": False,
        }

    result = await search_courses(value)
    courses = result.get("courses", []) if isinstance(result, dict) else []
    if not courses:
        return {
            "class_name_or_id": class_name_or_id,
            "matches": [],
            "match_count": 0,
            "exact_match": None,
            "ambiguous": False,
            "message": "No Moodle course found for that CRM class name.",
        }

    if len(courses) == 1:
        exact_match = courses[0]
    else:
        exact_matches = [
            course
            for course in courses
            if str(course.get("id")) == value
            or str(course.get("shortname", "")).lower() == value.lower()
            or str(course.get("fullname", "")).lower() == value.lower()
        ]
        exact_match = exact_matches[0] if len(exact_matches) == 1 else None

    return {
        "class_name_or_id": class_name_or_id,
        "matches": courses,
        "match_count": len(courses),
        "exact_match": exact_match,
        "ambiguous": exact_match is None,
    }


@app.tool()
async def get_course_content_by_name_or_id(class_name_or_id: str) -> Any:
    """
    Get a Moodle course's full contents/structure by CRM class name or course id.

    Text names are resolved the same way as get_class_details: content is only
    fetched when there is a single/exact course match, otherwise the candidate
    matches are returned so the caller can disambiguate instead of guessing.
    """
    value = class_name_or_id.strip()
    if value.isdigit():
        return await get_course_contents(int(value))

    class_details = await get_class_details(value)
    exact_match = class_details.get("exact_match") if isinstance(class_details, dict) else None
    if not exact_match:
        return {
            "class_name_or_id": class_name_or_id,
            "found_course": False,
            "matches": class_details.get("matches", []) if isinstance(class_details, dict) else [],
            "ambiguous": (
                class_details.get("ambiguous", True) if isinstance(class_details, dict) else True
            ),
            "message": (
                "No single matching Moodle course found for that name; "
                "specify a course id or refine the name."
            ),
        }

    return await get_course_contents(exact_match["id"])


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(
            "MCP_TRANSPORT must be one of: stdio, sse, streamable-http."
        )

    auth_token = os.getenv("MCP_AUTH_TOKEN", "")
    if transport == "streamable-http" and auth_token:
        import uvicorn
        from starlette.responses import JSONResponse

        class BearerAuthMiddleware:
            """Rejects HTTP requests that don't present the configured bearer token."""

            def __init__(self, asgi_app: Any, token: str) -> None:
                self.asgi_app = asgi_app
                self.expected = f"Bearer {token}"

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] != "http":
                    await self.asgi_app(scope, receive, send)
                    return

                headers = dict(scope.get("headers") or [])
                supplied = headers.get(b"authorization", b"").decode("latin-1")
                if not hmac.compare_digest(supplied, self.expected):
                    response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return

                await self.asgi_app(scope, receive, send)

        secured_app = BearerAuthMiddleware(app.streamable_http_app(), auth_token)
        uvicorn.run(secured_app, host=app.settings.host, port=app.settings.port)
    else:
        if transport == "streamable-http":
            print(
                "WARNING: MCP_AUTH_TOKEN is not set; the streamable-http endpoint "
                "is unauthenticated.",
                file=sys.stderr,
            )
        app.run(transport=transport)
