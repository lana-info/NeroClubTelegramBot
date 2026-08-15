<?php
/**
 * Plugin Name: Nero Club Access
 * Description: Secure, self-hosted WordPress access bridge for the Nero Club subscription backend.
 * Version: 0.1.0
 * Requires PHP: 8.0
 */

defined('ABSPATH') || exit;

const NCAC_API_NAMESPACE = 'nero-club/v1';
const NCAC_ACCESS_META = '_nero_club_access_blocked';

function ncac_secret() {
    return defined('NERO_CLUB_SHARED_SECRET') ? (string) NERO_CLUB_SHARED_SECRET : '';
}

function ncac_request_is_authorized(WP_REST_Request $request) {
    $secret = ncac_secret();
    $timestamp = (string) $request->get_header('x-nero-timestamp');
    $signature = (string) $request->get_header('x-nero-signature');

    if ($secret === '' || $timestamp === '' || $signature === '' || !ctype_digit($timestamp)) {
        return false;
    }
    if (abs(time() - (int) $timestamp) > 300) {
        return false;
    }

    $expected = hash_hmac('sha256', $timestamp . '.' . $request->get_body(), $secret);
    return hash_equals($expected, $signature);
}

function ncac_permission_callback(WP_REST_Request $request) {
    return ncac_request_is_authorized($request)
        ? true
        : new WP_Error('nero_club_forbidden', 'Unauthorized integration request.', ['status' => 401]);
}

function ncac_allowed_roles() {
    return apply_filters('nero_club_allowed_roles', ['subscriber']);
}

function ncac_destroy_sessions($user_id) {
    if (class_exists('WP_Session_Tokens')) {
        WP_Session_Tokens::get_instance((int) $user_id)->destroy_all();
    }
}

function ncac_find_user(WP_REST_Request $request) {
    $user_id = absint($request->get_param('user_id'));
    if ($user_id > 0) {
        return get_user_by('id', $user_id);
    }

    $email = sanitize_email((string) $request->get_param('email'));
    if ($email !== '') {
        return get_user_by('email', $email);
    }

    $username = sanitize_user((string) $request->get_param('username'), true);
    return $username !== '' ? get_user_by('login', $username) : false;
}

function ncac_sync_user(WP_REST_Request $request) {
    $idempotency_key = sanitize_text_field((string) $request->get_header('x-nero-idempotency-key'));
    if ($idempotency_key === '') {
        return new WP_Error('nero_club_missing_idempotency_key', 'Idempotency key is required.', ['status' => 422]);
    }

    $cache_key = 'ncac_idem_' . hash('sha256', $idempotency_key);
    $cached = get_transient($cache_key);
    if (is_array($cached)) {
        return new WP_REST_Response($cached, 200);
    }

    $action = sanitize_key((string) $request->get_param('action'));
    $allowed_actions = ['create_or_activate', 'deactivate', 'restore'];
    if (!in_array($action, $allowed_actions, true)) {
        return new WP_Error('nero_club_invalid_action', 'Unsupported access action.', ['status' => 422]);
    }

    $user = ncac_find_user($request);
    $password = (string) $request->get_param('password');
    $role = sanitize_key((string) $request->get_param('role'));
    if ($role === '') {
        $role = 'subscriber';
    }
    if (!in_array($role, ncac_allowed_roles(), true)) {
        return new WP_Error('nero_club_invalid_role', 'Role is not allowed.', ['status' => 422]);
    }

    if ($action === 'create_or_activate' && !$user) {
        $username = sanitize_user((string) $request->get_param('username'), true);
        $email = sanitize_email((string) $request->get_param('email'));
        if ($username === '' || !is_email($email) || strlen($password) < 16) {
            return new WP_Error('nero_club_invalid_user', 'Username, email and a strong password are required.', ['status' => 422]);
        }
        $created = wp_create_user($username, $password, $email);
        if (is_wp_error($created)) {
            return $created;
        }
        $user = get_user_by('id', $created);
    }

    if (!$user instanceof WP_User) {
        return new WP_Error('nero_club_user_not_found', 'WordPress user was not found.', ['status' => 404]);
    }

    if ($action === 'create_or_activate') {
        $user->set_role($role);
        if ($password !== '') {
            if (strlen($password) < 16) {
                return new WP_Error('nero_club_weak_password', 'Password must contain at least 16 characters.', ['status' => 422]);
            }
            wp_set_password($password, $user->ID);
        }
        update_user_meta($user->ID, NCAC_ACCESS_META, '0');
    } elseif ($action === 'deactivate') {
        update_user_meta($user->ID, NCAC_ACCESS_META, '1');
        ncac_destroy_sessions($user->ID);
    } elseif ($action === 'restore') {
        update_user_meta($user->ID, NCAC_ACCESS_META, '0');
    }

    $result = [
        'user_id' => (int) $user->ID,
        'login' => (string) $user->user_login,
        'action' => $action,
        'password_set' => $action === 'create_or_activate' && $password !== '',
        'access_blocked' => get_user_meta($user->ID, NCAC_ACCESS_META, true) === '1',
    ];
    set_transient($cache_key, $result, DAY_IN_SECONDS);
    return new WP_REST_Response($result, 200);
}

function ncac_block_login($user, $username, $password) {
    if ($user instanceof WP_User && get_user_meta($user->ID, NCAC_ACCESS_META, true) === '1') {
        return new WP_Error('nero_club_access_blocked', 'Website access is inactive.');
    }
    return $user;
}
add_filter('authenticate', 'ncac_block_login', 30, 3);

add_action('rest_api_init', function () {
    register_rest_route(NCAC_API_NAMESPACE, '/users/sync', [
        'methods' => WP_REST_Server::CREATABLE,
        'callback' => 'ncac_sync_user',
        'permission_callback' => 'ncac_permission_callback',
        'args' => [
            'action' => ['required' => true, 'type' => 'string'],
            'user_id' => ['required' => false, 'type' => 'integer'],
            'username' => ['required' => false, 'type' => 'string'],
            'email' => ['required' => false, 'type' => 'string', 'format' => 'email'],
            'role' => ['required' => false, 'type' => 'string'],
            'password' => ['required' => false, 'type' => 'string'],
        ],
    ]);
});
