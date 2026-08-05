/**
 * PLAN B - 학습작업 fetch API 클라이언트
 * API 명세서.pdf (생성일 2026-08-03) 계약 그대로 구현함.
 * BE3가 /exams/tasks/create/, /exams/tasks/<id>/update/, /exams/tasks/<id>/delete/
 * 를 아직 연결 전이라고 문서에 적어놨어서, 이 파일은 그 API가 붙기 전까지는
 * 실제로 호출하면 404가 날 수 있음. URL/요청 형식은 문서와 100% 맞춰뒀음.
 */

function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
}

async function requestTaskApi(url, data = null) {
    const options = {
        method: 'POST',
        headers: {
            'X-CSRFToken': getCsrfToken(),
            'Content-Type': 'application/json',
        },
    };
    if (data !== null) {
        options.body = JSON.stringify(data);
    }

    const response = await fetch(url, options);
    const body = await response.json();

    if (!response.ok || !body.isSuccess) {
        throw body;
    }
    return body.result;
}

/** 학습작업 추가. exam_id 필수. */
function createTask(payload) {
    return requestTaskApi('/exams/tasks/create/', payload);
}

/** 학습작업 수정. exam_id는 보내지 않음(수정 모달에서 과목은 읽기 전용). */
function updateTask(taskId, payload) {
    return requestTaskApi(`/exams/tasks/${taskId}/update/`, payload);
}

/** 학습작업 삭제. */
function deleteTask(taskId) {
    return requestTaskApi(`/exams/tasks/${taskId}/delete/`);
}