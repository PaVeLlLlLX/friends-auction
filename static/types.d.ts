/** Application and wire contracts for the plain JavaScript client.
 * These declarations describe the Flask API; they do not validate received JSON.
 * Money uses integer rubles and timestamps use Unix seconds.
 */
type UserStatus = 'new' | 'pending' | 'approved' | 'rejected';
type DemoRole = 'guest' | 'friend' | 'other' | 'admin';
type AdminTab = 'lots' | 'archive' | 'applications' | 'new';
type AdminView = AdminTab | 'lot' | 'edit';
type Fulfillment = '' | 'agreed' | 'delivered';

/** Identity and permissions returned to the authenticated user, without private names. */
interface User {
  first_name: string;
  username: string | null;
  status: UserStatus;
  admin: boolean;
}

/** Public lot plus subscription and leading-bid flags scoped to the requesting user. */
interface Lot {
  id: number;
  revision: number;
  title: string;
  description: string;
  condition: string;
  start_price: number;
  step: number;
  price: number | null;
  count: number;
  photos: string[];
  status: 'active' | 'cancelled' | 'ended';
  leading: boolean;
  has_bid: boolean;
  participation: boolean;
  watching: boolean;
}

/** Private applicant identity; only administrator responses include these fields. */
interface PrivateIdentity {
  id: number;
  username: string | null;
  first_name: string;
  applicant_given_name: string;
  applicant_family_name: string;
}
interface Applicant extends PrivateIdentity {
  status: Exclude<UserStatus, 'new'>;
}
/** An eligible participant's highest bid, ordered by amount by the server. */
interface Candidate extends PrivateIdentity {
  amount: number;
}
/** One accepted bid, including the private bidder identity, for administrator views. */
interface AdminBid extends PrivateIdentity {
  amount: number;
  created: number;
}
/** An accepted bid belonging to the current user; no other participant identity is exposed. */
interface MyBid {
  id: number;
  lot_id: number;
  title: string;
  amount: number;
  created: number;
  deleted: boolean;
  status: 'active' | 'cancelled' | 'ended';
  own_leading: boolean;
}

/** Delivery receipt for the organizer's currently relevant candidate notification. */
interface CandidateNotification {
  candidate_id: number;
  state: 'sending' | 'sent' | 'unknown' | 'not_sent';
}

interface MeResponse {
  user: User | null;
  end: number;
  demo: boolean;
  trial: boolean;
}
interface LotsResponse {
  lots: Lot[];
  end: number;
}
interface MyBidsResponse {
  bids: MyBid[];
}
interface ApplicationsResponse {
  users: Applicant[];
}
interface AdminLotResponse {
  lot: AdminLot;
  candidates: Candidate[];
  bids: AdminBid[];
  has_refusals: boolean;
  fulfillment: Fulfillment;
  candidate_notification: CandidateNotification | null;
}
/** Archived lots remain visible only through organizer-authorized endpoints. */
interface AdminLot extends Lot {
  deleted: boolean;
}
interface AdminLotsResponse {
  lots: AdminLot[];
}
interface OkResponse {
  ok: true;
}
interface LotResponse {
  lot: Lot;
}

/** Read endpoints are mapped to their precise successful response type. */
interface ApiGetResponses {
  '/api/me': MeResponse;
  '/api/lots': LotsResponse;
  '/api/my-bids': MyBidsResponse;
  '/api/admin/applications': ApplicationsResponse;
  '/api/admin/lots': AdminLotsResponse;
  [path: `/api/admin/lots/${number}`]: AdminLotResponse;
}

/** Mutation bodies retain the concurrency fields required by the server. */
interface ApiPostBodies {
  '/api/application': { given_name: string; family_name: string };
  '/api/admin/lots': FormData;
  [path: `/api/lots/${number}/bid`]: {
    amount: number;
    expected_price: number | null;
    expected_revision: number;
  };
  [path: `/api/lots/${number}/subscription`]: {
    kind: 'watching' | 'participation';
    enabled: boolean;
  };
  [path: `/api/admin/users/${number}`]: { status: 'approved' | 'rejected' };
  [path: `/api/admin/lots/${number}`]:
    { action: 'cancel' | Fulfillment } | { action: 'refuse'; expected_candidate_id: number };
  [path: `/api/admin/lots/${number}/edit`]: {
    title: string;
    description: string;
    condition: string;
    start_price: number;
    step: number;
  };
  [path: `/api/admin/lots/${number}/notify-candidate`]: { expected_candidate_id: number };
  [path: `/api/admin/lots/${number}/candidate-notification/resolve`]: {
    expected_candidate_id: number;
    outcome: 'sent' | 'not_sent';
  };
  [path: `/api/admin/lots/${number}/delete`]: { expected_bid_count: number };
}
interface ApiPostResponses {
  '/api/application': { status: UserStatus };
  '/api/admin/lots': LotResponse;
  [path: `/api/lots/${number}/bid`]: LotResponse;
  [path: `/api/lots/${number}/subscription`]: OkResponse;
  [path: `/api/admin/users/${number}`]: OkResponse;
  [path: `/api/admin/lots/${number}`]: OkResponse;
  [path: `/api/admin/lots/${number}/edit`]: OkResponse;
  [path: `/api/admin/lots/${number}/notify-candidate`]: OkResponse;
  [path: `/api/admin/lots/${number}/candidate-notification/resolve`]: OkResponse;
  [path: `/api/admin/lots/${number}/delete`]: OkResponse;
}

type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
type ApiReadOptions = Omit<RequestInit, 'body' | 'method' | 'headers'> & {
  headers?: Record<string, string>;
};
type ApiRequestOptions = Omit<RequestInit, 'body' | 'headers'> & {
  headers?: Record<string, string>;
  body?: { [key: string]: JsonValue } | FormData;
};
/** Transport errors mark writes whose server outcome cannot safely be inferred. */
interface RequestFailure extends Error {
  outcomeUnknown?: boolean;
}

/** A single create intent retains its exact payload and ID until its result is known. */
interface CreateAttempt {
  operationId: string;
  payload: FormData;
  unknown: boolean;
  pending: boolean;
}

/** Stable control identity used when a background refresh must replace view nodes. */
interface FocusSnapshot {
  id: string;
  photo: string | null;
  user: string | null;
  status: string | null;
}

/** Authoritative snapshots and local view/cache bookkeeping; drafts remain in the DOM. */
interface AppState {
  me: User | null;
  lots: Lot[];
  demo: boolean;
  trial: boolean;
  role: DemoRole;
  end: number;
  adminView: AdminView | null;
  adminLotId: number | null;
  adminTarget: { id: number; edit: boolean } | null;
  adminLotsSignature: string | null;
  applications: Applicant[] | null;
  adminLotSignature: string | null;
  myBidsSignature: string | null;
}
/** Route-specific poll payloads stay distinct until their matching view consumes them. */
type PollExtra =
  | { kind: 'my'; data: MyBidsResponse }
  | { kind: 'applications'; data: ApplicationsResponse }
  | { kind: 'lot'; data: AdminLotResponse }
  | { kind: 'adminLots'; data: AdminLotsResponse }
  | null;
type DetailSnapshot = [Lot, UserStatus | null, boolean];

/** Only the Telegram Mini App SDK surface used by this application is declared. */
interface TelegramWebApp {
  initData: string;
  isActive?: boolean;
  ready(): void;
  expand(): void;
  onEvent?(event: 'activated', callback: () => void): void;
}
interface Window {
  Telegram?: { WebApp?: TelegramWebApp };
}
