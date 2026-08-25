"""
Reusable serializer helpers shared across apps.
"""
from .uploads import resolve_stored_url, store_upload


class ImageUploadMixin:
    """Let one create/edit endpoint accept EITHER a URL or an uploaded file.

    Each image is stored as a plain URL on the model. To also accept a file
    upload, declare a write-only file field on the serializer and map it to the
    URL field it should populate (plus the storage folder):

        class CustomerSerializer(ImageUploadMixin, serializers.ModelSerializer):
            avatar_file = serializers.ImageField(write_only=True, required=False)
            image_upload_fields = {"avatar_file": ("avatar", "avatars")}

    On create/update, any uploaded file is pushed to storage (Backblaze B2 when
    configured, else local) and the resulting URL written to the target field.
    Send `avatar` directly to set a URL, or `avatar_file` to upload — same call.

    MRO note: list this mixin BEFORE `serializers.ModelSerializer`.
    """

    #: {file_field_name: (target_url_field, storage_folder)}
    image_upload_fields: dict = {}

    def _apply_uploads(self, validated_data):
        request = self.context.get("request")
        for file_field, (url_field, folder) in self.image_upload_fields.items():
            uploaded = validated_data.pop(file_field, None)
            if uploaded:
                validated_data[url_field] = store_upload(uploaded, folder, request)
            else:
                # Clients echo back the signed URL they were served; storing it
                # would freeze an expiring link. Keep the stable stored value.
                val = validated_data.get(url_field)
                if isinstance(val, str) and "X-Amz-" in val:
                    validated_data.pop(url_field, None)
        return validated_data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for _file_field, (url_field, _folder) in self.image_upload_fields.items():
            if url_field in data:
                data[url_field] = resolve_stored_url(data[url_field])
        return data

    def create(self, validated_data):
        validated_data = self._apply_uploads(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data = self._apply_uploads(validated_data)
        return super().update(instance, validated_data)
