# 📄 AUTR 자동매매 시스템  
# 고정 레이어 설계 계약서 v0.2  
*(Stability First Architecture)*

---

## 1. 문서 목적

본 문서는 AUTR 자동매매 시스템의 **고정 레이어(변경 시 비용이 큰 영역)**를 정의한다.

이 문서에 정의된 계약은:

- 전략/ML/지표 변경
- 주문 방식 변경
- 프론트엔드 도입 여부 변경

과 무관하게 유지되어야 한다.

---

## 2. 저장소 구조 (Repository Architecture)

### 2.1 Git Repository 분리

- Backend: `autr-back`
- Frontend: `autr-front` *(선택적, pluggable)*

#### 원칙

- Backend는 완전 독립적으로 운영 가능해야 한다.
- Discord 알림만으로도 운영 가능해야 한다.
- Frontend는 대시보드/분석/UI 확장용이며 필수 요소가 아니다.

> Frontend는 가변 레이어(pluggable)이며, Backend 안정성과 무관해야 한다.

---

## 3. 시스템 책임과 권위 계약

### 3.1 최종 권위자 (Authority)

**Executor가 유일한 최종 권위자다.**

- 주문 실행
- 주문 취소
- 주문 수정
- 포지션 상태 변경
- Reconcile 수행

은 Executor만 수행한다.

Collector / Strategy는 주문을 직접 실행하지 않는다.

---

### 3.2 서비스 경계

#### Collector
- 1분봉 데이터 수집
- 데이터 저장
- Heartbeat 기록

#### Strategy
- 데이터 기반 Signal 생성 (제안)
- Signal Queue 전달
- Heartbeat 기록

#### Executor (최종 권위)
- Signal 수신
- Lock 획득
- Idempotency 검사
- 리스크 검사
- 주문 실행
- 상태머신 관리
- Reconcile 수행
- Heartbeat 기록

#### Watchdog
- Heartbeat 모니터링
- 서비스 재시작
- Discord 알림

---

## 4. 데이터 계층 계약

### 4.1 Source of Truth

- **Postgres = 영속/권위 데이터 저장소**
  - signals
  - positions
  - orders
  - executions
  - pnl

- **Redis = 운영 제어 계층**
  - Signal Queue
  - Symbol Lock
  - Heartbeat

Redis는 영속 데이터의 권위가 아니다.

---

## 5. 주문 안전 계약 (Idempotency)

### 5.1 Signal ID 생성 규칙

```text
signal_id = hash(
    symbol,
    side,
    timeframe,
    candle_close_time,
    strategy_version
)
```

#### 규칙

- 동일 입력은 반드시 동일 signal_id를 생성해야 한다.
- Executor는 이미 처리한 signal_id를 재처리하지 않는다.

---

### 5.2 Client Order ID 규칙

```text
client_order_id = f"{symbol}:{signal_id}:{leg}:{attempt}"
```

- leg ∈ {entry, exit}
- attempt ∈ {0,1,2}

#### 규칙

- 동일 client_order_id로 중복 주문 금지
- attempt는 최대 2회까지 허용

---

### 5.3 Symbol Lock

- key: `lock:executor:{symbol}`
- Executor는 주문/상태 변경 전에 Lock 획득 필수
- Lock 없이는 주문 금지

---

## 6. 주문 실행 정책

### 6.1 OrderExecutionPolicy

주문 정책은 교체 가능(pluggable) 구조로 설계한다.

현재 기본 정책:

```text
MarketOrderPolicy
```

지정가/메이커 정책은 향후 추가 가능하며 고정 레이어가 아니다.

---

## 7. 포지션 상태 머신

### 7.1 최소 상태 정의

```text
NONE → ENTERING → OPEN → EXITING → NONE
```

#### 상태 설명

- NONE: 포지션 없음
- ENTERING: 진입 주문 발행
- OPEN: 진입 체결 완료
- EXITING: 청산 주문 발행

---

## 8. Reconcile 계약 (재시작 복구)

Executor는 재시작 시 반드시 Reconcile을 수행한다.

### 8.1 기본 정합성 규칙

#### Case 1
DB = OPEN  
Exchange = NONE  
→ DB를 NONE으로 복구 + Discord 알림

#### Case 2
DB = NONE  
Exchange = OPEN  
→ DB를 OPEN으로 복구 + Discord 알림

#### Case 3
DB = ENTERING / EXITING  
→ 주문 상태 동기화 후 정책 적용

---

## 9. ENTERING Reconcile 정책 (MarketOrder)

### 9.1 절차

1. 거래소 포지션 조회
2. 거래소 활성 주문 조회

#### 조건 분기

- **포지션 존재** → 상태를 OPEN으로 전이
- **활성 주문 존재** → 주문 상태 동기화 후 대기
- **포지션 없음 + 활성 주문 없음** → 재주문 판단

---

### 9.2 재주문 정책

#### 가격 이탈 검사

```text
price_diff = abs(current_price - signal_price) / signal_price
```

- price_diff > 1%
  - 재주문 금지
  - 상태 NONE 복귀
  - Discord 알림 ("가격 이탈로 스킵")

- price_diff ≤ 1%
  - Market Order 재요청

---

### 9.3 재시도 제한

- 최대 2회 재시도
- attempt 0 → 최초 주문
- attempt 1 → 30초 후 재시도
- attempt 2 → 30초 후 최종 재시도
- 이후 실패 시 NONE 복귀 + 알림

---

## 10. EXITING 정책

EXITING도 ENTERING과 동일한 규칙을 따른다.

- 포지션 없으면 NONE
- 주문 없고 포지션 존재 시 가격이탈 검사
- 1% 초과 시 청산 스킵 + 알림
- 2회 재시도 제한

---

## 11. Heartbeat & Watchdog

### 11.1 Heartbeat 키

- hb:collector
- hb:strategy
- hb:executor

### 11.2 기준

- 일정 시간 이상 갱신되지 않으면 watchdog가 재시작
- 재시작 시 Discord 알림

Health 기준은 “프로세스 생존”이 아니라 “진행”이다.

---

## 12. 알림 정책

Discord 알림 대상:

- 진입 체결
- 청산 체결
- 재시작 발생
- Reconcile 발생
- 가격 이탈 스킵 발생

모든 로그는 DB 저장.

---

## 13. 변경 허용 범위

다음은 고정 레이어에 포함되지 않음:

- 전략 추가/삭제
- ML/AI 파라미터 튜닝
- WebSocket 전환
- Frontend 대시보드
- 주문 정책 교체 (지정가 도입)

---

## 14. 안정화 우선 실행 순서

1. autr-back 레포 정리
2. Docker compose 구성
3. Executor에 idempotency + lock 구현
4. MarketOrderPolicy 적용
5. Reconcile 구현
6. Heartbeat + Watchdog 적용
7. Postgres/Redis 점진 도입

---

## 📌 결론

AUTR 자동매매 시스템은:

- Executor 중심 권위 모델
- Idempotent 주문 설계
- MarketOrder 기반 단순 안정화
- Reconcile 기반 복구 가능 구조
- Docker + Watchdog 운영

을 고정 레이어로 정의한다.

이 문서는 전략/AI 확장과 무관하게 유지되어야 한다.
