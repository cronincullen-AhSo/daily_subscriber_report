# frozen_string_literal: true
# typed: false

class ReportMailer < ApplicationMailer
  def daily_subscribers(to:, subject:, csv:, filename:, date_str:,
                        new_signups_count:, new_subs_count:,
                        max_actor_id:, prev_max_actor_id:, id_range:)
    attachments[filename] = { mime_type: 'text/csv', content: csv }
    @date_str           = date_str
    @new_signups_count  = new_signups_count
    @new_subs_count     = new_subs_count
    @max_actor_id       = max_actor_id
    @prev_max_actor_id  = prev_max_actor_id
    @id_range           = id_range
    mail(to: to, subject: subject)
  end
end
