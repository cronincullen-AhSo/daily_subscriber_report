# frozen_string_literal: true
# typed: false

require 'csv'

namespace :reports do
  desc 'Email daily new profiles, trials, and subscribers CSV to marketing'
  task daily_subscribers: :environment do
    since     = 24.hours.ago.utc
    recipient = ENV.fetch('REPORT_RECIPIENT', 'taylor.shurte@castability.actor')
    date_str  = Date.today.strftime('%B %-d, %Y')
    rows      = []

    new_actors = Actor
      .joins(:user)
      .where(created_at: since..)
      .order(:id)
      .select('actors.id as actor_id','actors.created_at','actors.has_external_subscription','users.first_name','users.last_name','users.email')

    new_actors.each do |actor|
      type = actor.has_external_subscription ? 'Subscriber' : 'Trial / Free'
      rows << [actor.actor_id, actor.first_name, actor.last_name, actor.email, type, actor.created_at.strftime('%Y-%m-%d %H:%M UTC')]
    end

    new_conversions = Actor
      .joins(:user)
      .where(has_external_subscription: true)
      .where(updated_at: since..)
      .where('actors.created_at < ?', since)
      .order(:id)
      .select('actors.id as actor_id','actors.updated_at','users.first_name','users.last_name','users.email')

    new_conversions.each do |actor|
      rows << [actor.actor_id, actor.first_name, actor.last_name, actor.email, 'Converted → Subscriber', actor.updated_at.strftime('%Y-%m-%d %H:%M UTC')]
    end

    max_actor_id      = Actor.maximum(:id) || 0
    prev_max_actor_id = Actor.where('created_at < ?', since).maximum(:id) || 0
    min_new_id        = new_actors.first&.actor_id
    max_new_id        = new_actors.last&.actor_id
    id_range          = min_new_id && max_new_id ? "#{min_new_id}–#{max_new_id}" : 'none today'

    csv_data = CSV.generate(headers: true) do |csv|
      csv << ['Actor ID', 'First Name', 'Last Name', 'Email', 'Type', 'Timestamp (UTC)']
      rows.each { |row| csv << row }
    end

    ReportMailer.daily_subscribers(
      to: recipient,
      subject: "Castability — New Signups #{date_str} (#{rows.length})",
      csv: csv_data,
      filename: "castability_signups_#{Date.today.iso8601}.csv",
      date_str: date_str,
      new_signups_count: new_actors.length,
      new_subs_count: new_conversions.length,
      max_actor_id: max_actor_id,
      prev_max_actor_id: prev_max_actor_id,
      id_range: id_range
    ).deliver_now

    puts "Sent #{rows.length} records to #{recipient} | IDs #{id_range} | Platform max: #{max_actor_id}"
  end
end
